# Design decisions

## 1. Delivery guarantee: at-least-once + idempotent completion
Redis Streams redelivers unacknowledged messages (XAUTOCLAIM), so a job can run more than once. The external call (LLM) has no side effects in our system, so repeating it is safe. Completion is one Postgres transaction: update the job `WHERE status != 'succeeded'`, insert the output (unique on job_id), check the affected row count, commit, then XACK. A crash between commit and XACK causes redelivery, the guarded update matches nothing, and nothing is duplicated. Result: effectively-once. We never claim exactly-once delivery.

## 2. Transactional outbox
The API writes the job and an outbox row in one transaction. A dispatcher publishes outbox rows to the stream. This avoids losing a job between the database write and the queue publish.

### Dispatcher crash behaviour
`publish_pending` does XADD and then marks rows published inside one Postgres transaction. Crash before commit: rows stay unpublished and are sent again (a duplicate in the stream, which is safe because completion is idempotent). Redis down: the transaction rolls back and nothing is marked published, so no job is lost. `FOR UPDATE SKIP LOCKED` lets several dispatchers run without taking the same row.

## 3. Redis Streams vs Kafka
(Fill in: why Streams fits this scale, consumer groups, simplicity, what would change at larger scale.)

## 4. Mock LLM provider
Public demo and CI use a mock provider with latency and 429/500/timeouts, so chaos tests are reproducible and visitors cannot run up an API bill. Real Gemini is used for the planner and a capped number of worker calls.

## 5. Worker flow (Day 4)
`app/worker.py` runs N asyncio tasks (`worker-1..N`) in one Redis consumer group, each doing `XREADGROUP ... ">"` with a short block timeout. For each message:
1. If `jobs.status` is already `succeeded` (or the job is gone), XACK and skip.
2. Claim: `UPDATE jobs SET status='running', locked_by, started_at, attempts=attempts+1 WHERE id=$1 AND status != 'succeeded'` plus a `job.started` event in one transaction. Zero rows: XACK and skip.
3. Call the provider (mock). On success, ONE transaction: guarded `UPDATE ... status='succeeded'`, and only if it matched a row, insert `job_outputs` (unique on job_id) and `job.succeeded`. Commit, THEN XACK.
4. On a provider error see #6 (retries and the DLQ).

Crash behaviour: `kill_worker` cancels the task. Only `ProviderError` and `TimeoutError` are caught, `CancelledError` is not, and no XACK is in a `finally`. A killed worker leaves the job `running` and the message pending, which is what the Day 6 XAUTOCLAIM recovery picks up.

Known limit (closed on Day 6, see #7): the claim only guards against `succeeded`. If the same job is delivered to two workers while the first is still `running`, both call the provider (at-least-once). Completion is still single-effect because the guarded UPDATE lets only one writer insert the output and the `job.succeeded` event. Day 6 (heartbeats, claim ownership) narrows this window.

## 6. Retries, backoff and the DLQ (Day 5)
**Classification.** `is_retryable` (pure, `app/retry.py`): timeouts and HTTP 429/500/502/503/504 are retryable; anything else (e.g. 400) is not and goes straight to the DLQ. Retrying an error that cannot succeed only wastes provider quota.

**Backoff.** `backoff_delay(attempt) = uniform(0, min(cap, base * 2**(attempt-1)))`, base 1s, cap 30s. Exponential growth gives a struggling provider room to recover; FULL jitter (random over the whole window) stops many jobs that failed together from retrying together. The function takes an injectable `rng`, so the tests seed it.

**The delay must not hold a worker.** On a retryable error below `max_attempts` (a column on the job, default 5), one transaction sets `status='retrying'` and writes `job.retry_scheduled`. The worker then does `ZADD jobs:delayed <due time> <job id>` and XACKs, and is free for the next message. A small async mover task (`move_due_jobs`, polling every 0.5s) takes due ids, reads the job row, XADDs it back to the `jobs` stream, and only then ZREMs it. XADD-before-ZREM means a crash can duplicate a delivery (safe, completion is idempotent) but never lose a retry. Cost: retries fire up to one poll interval late.

**Crash windows.** Commit then crash before ZADD: the message is still pending, so it is redelivered and the job is claimed again (Day 6 recovery). ZADD then crash before XACK: a duplicate delivery, harmless for the same reason.

**Dead jobs.** After `max_attempts` (or on a non-retryable error), ONE transaction sets `status='dead'`, writes `job.dead` and XADDs to the `dlq` stream (the XADD is before COMMIT, so a failed XADD rolls everything back and the message stays pending). A failed COMMIT after XADD can leave a duplicate DLQ entry. The DLQ stream is therefore at-least-once, and `jobs.status='dead'` is the source of truth. Then the worker XACKs the original message. `dead` is terminal like `succeeded`: stale deliveries of a dead job are acknowledged and skipped, which also means the claim guard is now `status NOT IN ('succeeded','dead')`.

**Replay.** `replay_dlq(job_id)` resets a `dead` job (`status='queued'`, `attempts=0`), inserts an outbox row and a `job.replayed` event in one transaction, and the dispatcher publishes it. Using the outbox instead of a direct XADD means a crash after the commit cannot lose the replayed job. It returns False for a job that is not dead. Matching entries are then removed from the DLQ stream, best effort.

## 7. Crash recovery: heartbeats, leases and XAUTOCLAIM (Day 6)
**Heartbeat = "is the process alive".** Each worker has its own heartbeat task (`app/recovery.py`) that refreshes the Redis key `worker:hb:<id>` with a TTL (default: every 2s, TTL 6s). It is a separate task so it keeps beating while the worker is awaiting a slow provider call. `kill_worker` cancels both the worker and its heartbeat and leaves the key to expire, like a real crash. A graceful `stop()` deletes the keys. No event is written per heartbeat.

**Lease = "how long a message may sit pending before anyone looks at it".** Default 15s, and it must be longer than the heartbeat TTL. Each worker loop runs a recovery step every `recovery_interval` (default 5s): `XAUTOCLAIM` messages idle longer than the lease, with itself as the consumer, then handles them like any delivery. Handing the message to the claimer means it goes to a worker that is alive by definition, with no separate dispatch step. A busy worker skips its recovery step, so another worker picks the message up.

**The lease alone never decides.** The claim (`_claim`) is one transaction that locks the job row (`SELECT ... FOR UPDATE`) and then:
- job gone, `succeeded` or `dead`: XACK and skip.
- `running` under another worker whose heartbeat key exists: skip WITHOUT XACK. This closes the Day 4 gap: a duplicate delivery no longer runs the provider while the first run is alive. If the owner later dies, the message is still pending and is reclaimed. If it finishes, the next delivery sees `succeeded` and just acks.
- `running` but the owner has no heartbeat, or the lock is this worker's own (a worker handles one message at a time, so a lock it holds is a previous life with the same id): take over. A `job.recovered` event records the previous and new worker ids, then the usual `job.started`.

The row lock makes two workers claiming at once take turns, so the second always sees the first's lock.

**Completion is unchanged in shape.** One transaction: `UPDATE ... WHERE status NOT IN ('succeeded','dead')`, insert the output only if a row matched, insert `job.succeeded`, commit, then XACK. If a worker dies between the commit and the XACK, the message is reclaimed after the lease, the claim sees `succeeded`, and it only acks: one output row, one `job.succeeded` event, provider not called again. A test hook (`after_complete`) lets a test stop a worker at exactly that point.

**Trade-offs.**
- A takeover counts as an attempt (`attempts + 1`). A job that keeps killing its workers therefore ends in the DLQ instead of crashing workers forever.
- A skipped duplicate stays pending and is reclaimed every lease interval until the owner finishes. That is some wasted XAUTOCLAIM traffic but no wasted provider calls.
- Heartbeats prove the process is alive, not that the job is making progress. A hung provider call would keep its job locked, which is why every call has a timeout (#8).
- If a worker is only paused (GC, network blip) past the heartbeat TTL, a second worker can take over and both may call the provider. Effective-once still holds because only one completion transaction matches.
- Reclaiming reads at most 10 messages per cycle. Stale consumers are not removed from the group (no XGROUP DELCONSUMER yet).

## 8. Provider timeout, config validation and the chaos test (Day 7)
**Timeout.** Every provider call runs under `asyncio.wait_for(..., settings.provider_timeout)` (default 30s). A hung call is cancelled and raises `TimeoutError`, which `is_retryable` already treats as retryable, so it follows the normal path: `retrying` with backoff, then the DLQ after `max_attempts`. Heartbeats only prove the process is alive, so without this a hung call kept its job locked forever. The mock has a `hang` mode (never answers) to test it. The 30s default is far above normal latency, so it only fires on a genuinely stuck call; the cost is that a legitimately slower call is retried instead of waited for.

**Startup validation.** `Settings` rejects `heartbeat_ttl >= lease_seconds` when it is created. If the heartbeat could outlive the lease, a dead worker would still look alive when its message becomes claimable, and recovery would skip the job until the key expired. A bad value now stops the app at startup instead of surfacing during a crash. `provider_timeout` must be positive.

**Chaos test.** `app/chaos.py` is the scenario and `tests/test_chaos.py` is the assertion: 200 mock jobs, 4 workers, a random worker killed and replaced every 0.15-0.4s (it prefers a worker holding a running job, so recoveries really happen; the test also asserts kills and recoveries occurred, so it cannot pass without chaos). Pass means every job `succeeded`, one `job_outputs` row and one `job.succeeded` event per job, and `XPENDING` 0. Jobs get `max_attempts=10` because a takeover counts as an attempt (#7). It uses short clocks (lease 0.6s, heartbeat TTL 0.4s) and takes about 4s, so it is marked `slow` but still runs in CI. `scripts/demo_chaos.py` runs the same code against the compose services and prints the summary used in the README. The workers run in the script's own process, so a "kill" cancels a task, the same crash model as the other tests, not a SIGKILL of a container.

## 9. Gemini planner as a queue job (Week 2, step 1)
**Planning is a job, not an API call.** `POST /goals` writes the goal, a `planner` job, its outbox row and `goal.created` in ONE transaction and returns at once with `status: planning`. A worker plans it. The request never waits on an LLM, and a slow or failing provider uses the same timeout / retry / DLQ / crash-recovery path as every other job (`ProviderError` from a Gemini 429 or 5xx is retryable, a 400 is not).

**Storing the plan is part of completion.** `_complete` takes an `on_success` hook that runs inside the same transaction as the guarded `UPDATE jobs ... WHERE status NOT IN (...)`. Only the delivery that wins that UPDATE stores the tasks, so a redelivered planner job cannot create them twice. `store_plan` also only plans a goal still in `planning`. It writes `goal.planned` and one `task.created` per task in that same transaction (events are never emitted without the state change). The planner's ids ("t1") are translated to real UUIDs, and `depends_on` is translated with them.

**Gemini output is untrusted.** `PlanModel` (Pydantic) enforces 3-8 tasks, unique ids, no unknown or self dependencies, at least one task with no dependencies, and no cycles (Kahn's algorithm). The model has no field defaults because Gemini's `response_schema` marks every field required. Invalid output (bad JSON or a bad graph) gets ONE retry, then a generic 3-step fallback plan, so a goal never gets stuck on a model that cannot produce a valid plan. A real API error is different: it is raised so the job retry logic handles it, instead of silently producing a fallback.

**Mock planner and the cap.** `MockPlanner` returns a fixed diamond plan with an approval task at the end, so tests and the demo exercise parallel tasks, ordering and approvals with no API. It is used when there is no key, and when the daily cap is hit. The cap (`app/budget.py`) is one Redis counter per UTC day, incremented atomically BEFORE each call, so concurrent workers cannot overshoot. Every Gemini call counts, including the retry. A refused call also increments the counter, which only makes it read a little above the limit. Default 50/day (`GEMINI_DAILY_CAP`).

**Adaptations.** The `tasks` table had no description, so migration 004 adds `tasks.description` and `tasks.key` (the planner's id, for display), plus `goals.plan_source` (gemini | mock | fallback) and `goals.planned_at`. The conftest Redis fixture now also clears the daily counters. The planner has no tests against real Gemini by design (CLAUDE.md): the tests use a fake client with the same `aio.models.generate_content` shape and assert the request asks for JSON with `response_schema=PlanModel`.

**Not yet done (later steps).** Enqueueing tasks (step 2), the same cap for worker LLM calls (step 4).

## 10. Running a goal: dependencies, approvals, failure (Week 2, step 2)
**State machine.** Task: `pending -> (awaiting_approval ->) queued -> succeeded | failed`, or `rejected` / `cancelled`. Goal: `planning -> planned -> running -> succeeded | failed`. (`planned` is only a moment: storing the plan also starts the goal.) Task jobs are normal jobs of type `task` (idempotency key `task:<id>`, so a task can never get two jobs), so retries, the DLQ and crash recovery all apply unchanged.

**Everything happens inside the completion transaction.** A task job's `on_success` hook (the same mechanism as the planner) marks the task `succeeded`, stores its output, and calls `advance_goal`, which enqueues every pending task whose dependencies are all `succeeded` (job + outbox row + `task.queued` in that transaction) or moves it to `awaiting_approval`. There is no separate "scheduler" process that could fall behind or crash between steps. The hook runs only for the delivery that wins the guarded `UPDATE jobs`, and `complete_task` itself only acts on a task still `queued`. So a redelivered completion cannot enqueue dependents twice (tested by running the completion three times).

**The goal row lock is load-bearing.** `advance_goal` first does `SELECT ... FROM goals FOR UPDATE`. Two sibling tasks finishing at the same moment would otherwise each see the other as still `queued` (neither commit is visible yet) and nobody would start the task that waits for both: a lost wakeup. With the lock the second transaction waits, then sees the first's commit. A test holds one transaction open while another completes the sibling, and fails if the lock is removed (checked). Lock order is job row, task row, goal row everywhere; approve and reject take the goal lock first but never hold a job lock, so there is no cycle.

**Dependency outputs go into the prompt.** The dependent's prompt is built when it is queued: goal, task name and description, then each dependency's output text. The outputs live in `tasks.output` (migration 005), not read back from `job_outputs`, so building a prompt needs no join through jobs.

**Approval.** A task with `needs_approval` stops at `awaiting_approval` (`task.awaiting_approval`) once its dependencies are done. `POST /tasks/{id}/approve` queues it; `/reject` marks it `rejected` and fails the goal. Both take the goal lock and return 409 unless the task is awaiting approval, so approving twice cannot create a second job.

**Failure.** A job that reaches the DLQ calls `job_died` inside the `_bury` transaction: a dead `task` job marks the task `failed` and a dead `planner` job fails the goal directly. `fail_goal` sets `goal.failed` with the reason and cancels every task that has not started (`task.cancelled` events). Tasks already running are left to finish; their completion is recorded but `advance_goal` ignores a failed goal. Trade-off: one dead task fails the whole goal, there is no partial result or retry-this-branch.

**Success.** When every task is `succeeded` the goal becomes `succeeded` with `goals.result` = each task's output in key order, plus `goal.succeeded`.

**Adaptations.** Migration 005 adds `tasks.output`, `goals.result` and `goals.error`. Task order in results uses the planner's key (`ORDER BY key`), fine for t1..t8 but not a true topological order. The step 1 tests now expect a `running` goal with its root task queued. Parallelism of tasks is real: the 3 workers pick up sibling jobs concurrently.
