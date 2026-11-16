# Design decisions

## 1. Delivery guarantee: at-least-once + idempotent completion
Redis Streams redelivers unacknowledged messages (XAUTOCLAIM), so a job can run more than once. The external call (LLM) has no side effects in our system, so repeating it is safe. Completion is one Postgres transaction: update the job `WHERE status != 'succeeded'`, insert the output (unique on job_id), check the affected row count, commit, then XACK. A crash between commit and XACK causes redelivery, the guarded update matches nothing, and nothing is duplicated. Result: at-least-once delivery plus idempotent completion gives one stored result per job. Delivery itself is not exactly-once.

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

## 11. Live events: outbox -> Redis stream -> WebSocket (Week 2, step 3)
**The `events` table is the outbox.** Every state change already inserts its event in the same transaction, so "no event lost, none without its change" holds by construction. Migration 006 adds `events.published_at`. `publish_events` (`app/event_publisher.py`) copies unpublished rows to the Redis stream `events` (XADD first, mark published in the same transaction after), exactly like the job dispatcher. A failed XADD rolls the transaction back, so everything is retried; a crash after XADD republishes the batch. The stream is therefore at-least-once, and each entry carries the Postgres event id so consumers drop duplicates. The stream is trimmed to about 10,000 entries (approximate MAXLEN): a recent-history buffer, while Postgres keeps the full log.

**Ordering.** A transaction-level advisory lock allows one publisher at a time, publishing in id order. Caveat I am accepting: two transactions that commit in the opposite order of their event ids can appear in commit order, so ids are not strictly increasing in the stream and consumers must not rely on that.

**Hub.** One reader does `XREAD BLOCK` on the stream and pushes each event into a bounded per-client queue. Each client has its own sender task and the hub never awaits a socket, so a slow client cannot slow the others. Per client: a batch is sent as soon as something is pending, then the sender waits 1/15 s, so at most about 15 messages per second however many events arrive, and many events share one message (max 200). If a queue reaches 1,000 the oldest events are dropped and the next message carries `dropped: n` so the client knows to refetch `/state`. If one send takes longer than 5 s the client is closed and removed. A client that disconnects is removed the same way. The cost of drop-oldest is that a stalled client loses history instead of the system holding unbounded memory.

**Snapshot without gaps.** On connect the endpoint calls `hub.register` (synchronous, no await), which records the hub's current stream position `join_id`. The snapshot's events come from the stream up to `join_id`; the hub delivers only events after it, so the two never overlap or leave a gap. The snapshot is `goals` (latest 20) with their `tasks`, `workers`, `queue` depth and the last 100 events, oldest first. Goals, tasks and workers are read from Postgres/Redis a moment later than `join_id`, so a few of them can already reflect newer events; applying a newer event to newer state is harmless as long as clients apply events idempotently. `GET /state` returns the same snapshot.

**Workers in the snapshot** come from live heartbeat keys (`worker:hb:*`) joined with the job each running worker holds. Queue depth is queued + retrying jobs from Postgres (plus running / succeeded / dead counts). Not yet included: throughput and latency percentiles (step 5 adds `/stats` to the snapshot).

**Not done on purpose.** WebSocket authentication and origin checks come with deployment (step 9). `/ws` is a read-only feed; clients send nothing. The hub starts reading at the newest event on API startup, so events published while the API was down only reach clients through snapshots.

## 12. Protection, limits and the free-tier profile (Week 2, step 4)
**Atomic token bucket (Lua).** `app/ratelimit.py`: one script reads the bucket, refills it for the elapsed time, takes a token if there is one and writes it back. Redis runs a script without interleaving other commands, so 300 concurrent callers sharing a bucket of 25 get exactly 25 (tested, with several bucket objects standing in for several processes). The clock comes from Redis `TIME`, not from the caller. Idle buckets expire after a full refill time. Used for the per-IP limit on `POST /goals` (burst 5, refill 0.1/s, 429 + `Retry-After`) and the provider limiter. The client IP is the socket address; `X-Forwarded-For` is only used when `TRUST_FORWARDED_FOR=true` (behind Render's proxy), because otherwise anyone could pick their own address.

**Circuit breaker (Lua, shared by all workers).** Per provider name (`llm`, `planner`): closed counts consecutive failures; at 5 it opens for 10s; then half-open lets exactly ONE probe through (tested with 10 concurrent callers); probe success closes, failure re-opens; a probe that never reports expires after 10s so a crashed worker cannot hold it half-open. Only retryable errors (429, 5xx, timeouts) count as failures: a 400 means the provider answered, so it counts as healthy. Transitions write `circuit.opened` / `circuit.closed` events; the event insert is a separate write right after the script, so a crash between the two could lose a display event but never the circuit state.

**Refused jobs are delayed, not failed.** Before claiming a job the worker asks `Protection.admit`: circuit first, then the token bucket. If refused it ZADDs the job to the delayed set (the retry mover returns it later) and XACKs. Claiming is what increments `attempts`, so a parked job keeps `attempts = 0` (tested): an outage of the provider does not push healthy jobs into the DLQ. The mover now returns any unfinished job, not only `retrying` ones. Trade-offs: a duplicate delivery of an already finished job still spends a token; a probe granted to a message that turns out finished is lost until it expires.

**`mock_*` payload fields.** They steer the mock provider (fail on purpose, sleep), so the public `POST /jobs` rejects them (400), except `mock_latency`, which is clamped to 10s. Trusted callers (the chaos endpoints, tests) go through `create_job(trusted=True)` and may set any; latency is clamped for them too, and the worker clamps again before calling the provider, so no path in the database can make a worker sleep longer than 10s. Test helpers now create jobs through that trusted function instead of the HTTP endpoint.

**Real Gemini for tasks, capped.** `RoutingProvider` (workers) calls Gemini only for `task` jobs under the daily cap and only when no mock mode or latency is set; flood/chaos jobs (`llm.mock`) never spend quota. Over the cap, or with no key, it answers with the mock. Planner and workers share the one daily counter.

**Live demo profile (`LIVE_DEMO=true`).** One switch sets: embedded workers (inside the API process), 2 workers, heartbeat every 10s with 30s TTL, lease 45s, recovery every 30s, `XREADGROUP BLOCK 5000`, dispatcher/publisher polling at 0.5s. Plus: idle workers back off up to 5s between reads (a new goal cuts the sleep short); the retry mover polls only while something is delayed (it waits on an event, max 60s); the event hub reads the stream only while a client is connected; and a **sleep gate**: after 5 minutes with no new goal the workers, heartbeats, dispatcher and publisher stop polling entirely, and the next goal (or flood) wakes them. Rough budget while awake: about 40 Redis commands a minute (about 2,400 an hour), so the 450K guard is roughly 190 awake-hours a month; asleep it is about zero. `/healthz` reports `sleeping` and `live`.

**Free-tier guards.** (1) A daily cap on goals (default 30, `LIVE_GOALS_DAILY_CAP`, 0 = unlimited) returns 429 with a plain-language message. (2) The Redis command budget: `CountingRedis` counts every command this process sends and `CommandBudget` adds them to a monthly Redis key (flushing only when 20+ commands piled up, so an idle system stays silent). At 90% of `REDIS_MONTHLY_LIMIT` (500,000) the API answers `POST /goals` with 503 "live unavailable" and `/healthz` says `live: false`; the frontend then falls back to the recorded demo. The count is our own commands only, so it undercounts anything else using the same Redis. The Redis URL stays configurable (`REDIS_URL`).

**Startup reconcile.** Postgres is the source of truth. On API/worker start, every job that is queued/running/retrying but is on neither the jobs stream nor the delayed set gets an outbox row and a `job.reconciled` event, so the dispatcher republishes it (idempotent completion makes a needless republish harmless; a second reconcile is a no-op while the outbox row waits). An empty events stream is refilled with the last 200 events. Running workers also survive a flush now: a `NOGROUP` error makes them recreate the consumer group. Cost: reconcile scans the whole jobs stream at startup (1 command per 1,000 entries); the stream is never trimmed yet, which matters for the free Redis memory limit (noted for step 9).

**Adaptations.** `/healthz` was added first as asked (`/health` stays). The Redis client is now a blocking-pool `CountingRedis`: the default pool raises "Too many connections" past 10 concurrent callers, and the concurrency test exposed it.

## 13. Chaos API, load generator and stats (Week 2, step 5)
**Chaos acts on the worker pool inside the API process.** `POST /chaos/kill` (named or random), `/chaos/start` (named, or a fresh id), `/chaos/mock` (failure rate, min/max latency, each clamped to 10s) and `/chaos/flood` (N mock jobs), plus `GET /chaos/status`. A kill is the same crash model as the tests: the worker and its heartbeat task are cancelled and the heartbeat key is left to expire, so recovery really runs. Replacement ids are never reused (`worker-4`, `worker-5`, ...). With workers in a separate process these endpoints answer 409; run the API with `EMBEDDED_WORKERS=true` (always on in the live demo). Every action writes an event (`worker.killed`, `worker.started`, `chaos.config`, `chaos.flood`) so the map and log show it.

**Mock jobs only, and bounded.** Flood creates `llm.mock` jobs through the trusted `create_job` path (never a task or planner job, never real Gemini). One flood is limited to `FLOOD_MAX` jobs: 2,000 locally, forced to 100 when `LIVE_DEMO=true` (even if someone configures more, tested). Flood keys are unique per request, so repeated floods add more jobs. The endpoints are public in the demo, so they share a per-IP token bucket (20 burst, 1/s) and are the only callers that may set `mock_*` fields.

**Load generator.** `app/loadgen.py` adds 1-3 mock jobs every 2 seconds (`LOAD_GENERATOR=true`, default off, forced off in the live demo because it would keep the sleep gate awake and burn the Redis budget).

**`GET /stats`.** From Postgres plus the worker heartbeats: `throughput_per_sec` (succeeded jobs in the last 60s / 60), totals for `succeeded`, `retries` (count of `job.retry_scheduled` events: a takeover after a crash is not counted as a retry) and `dead`, `queue_depth` (queued + retrying), `running`, `active_workers`, and `latency_ms` p50/p95/p99. Latency is end to end (creation to completion, so it includes queueing, retries and backoff) for jobs that succeeded in the last 5 minutes, using `percentile_cont` (linear interpolation: for 100..1000 ms the p95 is 955). The same object is in every WebSocket snapshot and `/state`. It is computed on demand with a few indexed queries (migration 007), not cached; fine for a demo, and a cost to revisit if many clients poll.

**Found by the tests.** `WorkerPool.stop()` crashed (`DEL` with no keys) after every worker had been killed. The new `events(type)` index also changed the row order of an unordered query in a step 4 test, which now has `ORDER BY id`.

## Step 6: frontend event store

**A pure reducer, with the network injected.** `web/src/store.ts` is `(state, message) -> (state, needSnapshot)`: no sockets, timers or React, so each rule is a plain unit test. `web/src/connection.ts` owns the WebSocket, reconnect backoff (1s doubling to 10s) and the `/state` fetch, with the socket, fetch and timers passed in so tests use fakes.

**Dedupe by event id.** The stream is at-least-once and a snapshot can overlap the live feed, so the same event can arrive twice. The Postgres id is the key.

**Order by (created_at, id), not arrival.** Events from two transactions that commit in the opposite order of their ids are published in commit order (see `event_publisher.py`), so arrival order is not time order. Time first, id as tie-break (timestamps are compared at millisecond precision, so same-millisecond events fall back to id).

**Gap detection uses the server's `dropped` count, not id holes.** Postgres sequence ids skip numbers when a transaction rolls back, so "id jumped" would give false alarms. The hub already tells a client when it dropped events for it; that, and a new connection, are the cases where we know we missed something. On `dropped` the client fetches `GET /state` (one request at a time; gaps during a fetch are covered by it). On reconnect it relies on the snapshot the server pushes first on every new socket, instead of asking twice.

**Snapshots merge their events, they do not replace the log.** A `/state` reply can land after newer live events; replacing would lose them. Overlap is dropped by id. The log is capped at 500 events and the seen-id set is rebuilt from it, so memory is bounded.

**CORS.** The `/state` refetch is a cross-origin fetch from the dev server (WebSockets are not subject to CORS). `CORS_ORIGINS` (default `http://localhost:5173`) lists the allowed origins; Step 9 sets it to the Vercel origin.

**Not in this step:** the PixiJS map, panels, and deriving goal/task state from events (the debug view shows the snapshot as of the last fetch; events only feed the log).

## Step 7: the city map

**Animation is driven only by the event store.** `web/src/scene.ts` is a pure model (no Pixi, no React) that changes ONLY in `sync()`, from events the store holds, and in `step(dt)`, which only advances what an event already set in motion: a worker walking to the place its event named, then home; a killed worker fading; the gate easing; a pulse decaying. Nothing starts, fails or succeeds on a timer, and a test proves it (30 simulated seconds with no events change nothing). Live mode and the recorded presets (step 8) both feed the same store, so the map cannot tell them apart.

**Event to picture.** `job.started` worker turns blue and walks to Downstream; `job.succeeded` green, to Done, +1 done; `job.retry_scheduled` orange, back to the Queue, +1 queued; `job.dead` red, to the DLQ, listed with a Replay button; `worker.killed` fades; `worker.started` / first sight of a worker id spawns a sprite; `circuit.opened` swings the gate shut on the road to Downstream, `circuit.closed` reopens it; `task.awaiting_approval` shows an approval card until the task is decided. Walking back to the yard after an errand is a continuation of that event, not invented activity. If events arrive faster than the walk, the latest event wins (the worker redirects) so the picture never lags the system by an unbounded amount.

**Counters come from events, corrected by snapshots.** Queue depth = snapshot value + events: `job.created`, `task.queued`, `job.retry_scheduled`, `job.replayed`, `job.reconciled`, `chaos.flood` add; `job.started` subtracts. Live testing showed jobs created by `POST /jobs` emitted no event, so the count drifted; the API now writes `job.created` in the same transaction as the job. Any snapshot (reconnect, or a gap) resets the counters, so drift cannot accumulate. Events already inside a snapshot are marked applied and not counted twice.

**Performance rules.** Every sprite and Text is built once; workers are a fixed pool of 32 reused objects (more workers than that are not drawn); the ticker callback allocates nothing; Text is only touched when its string changes; the store's events are applied in one batch per frame and only when the store changed; a stalled tab's dt is capped at 0.1s. 20,000 events plus 600 frames apply in well under 1.5s in a unit test, and headless Chromium with software WebGL holds 60fps under 600 events/min (a real GPU is not needed to meet it; the real 60fps claim is a manual check, not a CI gate).

**DOM for controls, canvas for the picture.** Chaos panel, DLQ replay and approval cards are real buttons over the map: keyboard and screen-reader accessible, trivially testable. The canvas is `aria-hidden`; a polite live region reads the scene as a sentence ("3 workers (2 busy) · 6 queued · ... circuit tripped"). Words follow the breaker's terms (tripped = circuit.opened), not the gate's: an early version said "circuit closed" while the gate was shut, which read backwards.

**New backend surface.** `POST /dlq/{job_id}/replay` (409 if the job is not dead; shares the chaos per-IP limit), `dead_jobs` in the snapshot (so Replay works after a page load), and the `job.created` event. The failure-rate slider follows `chaos.config` events, so a change from another tab or curl moves it.

**Tests.** Scene model (19), components (12), API client (5), plus a Playwright smoke test against a mock WebSocket (9) that runs in the web CI job. Verified by hand against the real backend with embedded workers: flood from the UI (CORS preflight included), real circuit trips, real dead jobs, Replay.

**Not done on purpose:** Kenney isometric art (flat 2D is cheap and reads clearly), event log / task checklist / throughput panels (later), recorded presets and the live/recorded toggle (step 8).

## Step 8: recorded presets and live mode

**Why both.** A free host sleeps when idle and takes up to a minute to wake, and a public demo has daily caps. A site that shows a blank page or an error in that minute is a bad first impression, and a site that only replays a video proves nothing. So the page starts instantly on a recorded trace (works with no backend at all) and, in the background, pings `GET /healthz`. When the backend answers it switches to the real thing. Both stay in the product: recorded is the always-available demo, live is the proof.

**Why the traces are recorded from the real engine.** A trace is only honest if the system really did it. `scripts/record_trace.py` runs the production code: the FastAPI app (called in-process, so the real `/goals`, `/chaos/*`, `/tasks/*/approve`, `/dlq/*/replay` endpoints), the dispatcher, the event publisher and a real worker pool, against real Postgres and Redis, with the mock LLM (deterministic, no API spend). Only the clocks are short. The events are read back from the Redis `events` stream, so the files hold exactly what `/ws` sends (`id`, `stream_id`, `type`, `payload`, `ts`) plus the opening snapshot. Nothing is hand-written, so the map cannot show something the engine does not do.

**A recording must prove what it claims.** `app/trace.py` has `check_trace`: the happy path has a multi-step DAG and no retries; the retries trace has at least two retries and no dead job; the chaos trace has `worker.killed`, `job.recovered`, and the recovered job has exactly one `job.succeeded` event; the DLQ trace has `job.dead`, a click on the approval gate, a `job.replayed` and a later `job.succeeded` for that job. Every trace also has at most one `job.succeeded` per job (idempotent completion). The recorder re-records (up to 5 takes, seeded differently) until the checks pass, and `tests/test_traces.py` runs the same checks on the committed files, so a trace cannot drift from its preset. Honest note: the kill is a simulated crash (the worker task is cancelled, as in the chaos tests), not a killed process.

**The map cannot tell recorded from live.** One `TownStore` holds the event store; the WebSocket `Connection` and the `Replayer` both push messages into it with `dispatch`. The replay sends the snapshot first and then events as `{type:"events"}` batches, the same shapes the server sends, so dedupe by id and ordering by (ts, id) are the store's, unchanged. The replayer also dedupes and sorts up front to build its schedule.

**Timing comes from the recording.** An event is delivered when `(ts - first ts) / speed` has passed on a clock that can be paused and re-anchored; events due at the same instant go out as one batch. Pause freezes the position, speed changes keep the position continuous, restart resets the store. No delay is invented. Time, timers and the sink are injected, so the tests drive a fake clock exactly.

**Switching clears the store.** `Controller` owns the choice. Every switch calls `store.reset()` first and stops the old source; a late message from a closed socket, a late `/state` answer, and a slow trace download that finishes after the visitor moved on are all ignored (a token and a stopped flag), so recorded and live events never share a view. Rules: first load plays the first preset; if the backend answers and the visitor has not chosen anything, the page switches to live by itself; once they choose a preset it never switches on its own again. If the live socket drops, it falls back to a recording with a message and does not bounce back by itself.

**Health state machine** (`web/src/health.ts`): `waking` (ping now, then every 5s) for up to 60s, then `offline` (every 15s in the background), `connected` on any good answer. `live:false` in `/healthz` (the free-tier Redis budget guard) counts as offline with a reason. Pills: "Recorded Trace (Waking Live Backend...)", "Live Backend Connected", "Recorded Trace (Live Backend Offline)". The pill reports the backend; a separate "Showing: ..." line says what the map is showing, because a recording can be playing while the backend is connected.

**Live-only controls are disabled, not hidden,** with the reason as visible text and a tooltip: the typed prompt, the chaos panel, DLQ Replay, the approval buttons and the click-a-worker shortcut. On a recording the approval card still appears on the map (it is in the events), but its buttons are disabled. A 429 from the API (daily live-goal cap, per-IP limit) shows the server's message plus "Pick a preset: they always work", and the page stays in live mode.

**Tests.** Replay engine (12), health state machine (10), controller and mode switching (14), controls (12), plus Playwright: no backend and play a preset, DLQ preset (page clock driven by the test, so "5.5 seconds of trace" is exact), pause/restart, offline after 60s, keyboard, mobile, and a mocked `/healthz` plus WebSocket that flips to live, clears recorded events, sends a prompt, shows a 429, and falls back when the socket drops. Backend: `tests/test_traces.py` validates the committed files against the schema and the preset checks.

**Limits.** Traces are about 4 to 10 seconds long, short on purpose (use 1x/2x/4x or Restart). The recorded snapshot's stats block is from an empty system. Regenerate with `python scripts/record_trace.py --reset` (it wipes the database tables, so point it at a scratch database).

## Step 9: free deployment

**Shape.** Vercel serves the static app (and the recorded traces). One Render free web service runs the whole backend in one process: API, dispatcher, event publisher and workers (`LIVE_DEMO` implies `EMBEDDED_WORKERS`). Neon is Postgres, Upstash is Redis. The Dockerfile runs exactly one uvicorn worker on purpose: workers and the chaos controls live in the process. `render.yaml` marks every secret `sync: false` so none is in git. `docs/DEPLOY.md` is the step list and the env var table.

**Migrations at startup.** A hosted database starts empty and we have no shell on it. `app/migrate.py` applies `migrations/*.sql` once each, recording them in `schema_migrations`, one transaction per file, under an advisory lock (two instances starting together cannot both apply a file, and a failing file leaves nothing half applied). It refuses a database that has tables but no history rather than guess which files ran. Off by default (docker compose loads the files locally), on in `render.yaml`.

**Neon.** Use the direct host: it needs nothing special beyond `sslmode=require`. A pooled host (`-pooler`, PgBouncer in transaction mode) breaks asyncpg's prepared statements, so `app/db.py` turns the statement cache off when it sees `-pooler` (or `DB_POOLER=true`). All entry points build the pool through `app/db.py`. Idle connections are dropped after 60s because a scale-to-zero database kills them.

**CORS is locked, and checked.** `CORS_ORIGINS` must be the Vercel origin. With `LIVE_DEMO=true` the app refuses to start if it is empty, `*`, not https, localhost, or has a trailing slash, or if `TRUST_FORWARDED_FOR` is off (every visitor would share one rate limit behind Render's proxy). A bad deploy fails loudly in the logs. CORS does not protect WebSockets (any page can open one), so in the live demo `/ws` also rejects a browser `Origin` that is not in the list (close code 1008). A request with no Origin (curl, tests) is allowed: this blocks other sites, not scripts, which the per-IP limits and caps already handle.

**A real bug, found by booting clean.** Building a fresh virtualenv from `pyproject.toml` and starting the live profile showed every idle worker poll failing with "Timeout reading": redis-py 8 times reads out after 5s by default, and the live profile blocks on `XREADGROUP` for exactly 5s. The earlier tests used short blocks, so they never saw it. Fix: `socket_timeout=30` (must exceed every BLOCK), plus `health_check_interval=30` and keepalive for Upstash dropping idle connections. Two tests fail without the fix (one real 5s blocking read, marked slow). Because a newer library changed a default under us, the image installs from `requirements.lock`, which pins the tested versions.

**Secret scan in CI.** `scripts/secret_scan.py` (about 100 lines, regexes over `git ls-files`) fails on API-key shapes, private keys, tokens, a tracked `.env`, and `user:password@host` URLs except local hosts and placeholders; findings are masked in the output. Chosen over a third-party action so it can be read and explained in an interview; its limits are stated in the file: it does not scan history (a committed-then-deleted secret must be rotated) and gitleaks is stronger.

**Not verified here.** No Docker and no hosted accounts on the development machine, so the image build, TLS to Neon and Upstash, and the Render and Vercel settings are unexercised; they follow the providers' docs and `docs/DEPLOY.md` says so. Verified: clean-venv boot with the live variables, migrations on an empty database, a goal run to success with approval, no worker errors, foreign-origin preflight refused.

## Step 10: benchmark and README

**What is measured, and where.** k6 against the LOCAL stack only, one API process with embedded workers, mock LLM (50 to 200 ms), Postgres and Redis on the same laptop. Free-tier numbers would measure someone else's shared CPU and network, so they are not quoted. `bench/run.sh` wipes a scratch database, starts the API, runs k6 (`bench/jobs.js`), waits for the queue to drain, then reads latency and throughput from Postgres (creation to completion), because k6 only sees HTTP. It also checks the guarantees on every run: one `job_outputs` row per job and one `job.succeeded` event per succeeded job.

**Three questions, three measurements.** Ingest (constant-rate `POST /jobs`: latency and errors), engine throughput (a burst, then drain time per worker count, compared with the ideal `workers / mean latency`), and correctness under chaos (the existing `demo_chaos.py`, three seeds). Keeping them separate stops a worker-bound number being mistaken for an API limit.

**Findings worth saying out loud.** Up to 8 workers the engine is worker-bound (about 96% of ideal). The ceiling is one Python process, about 100 jobs/s, and past it more workers make things worse (32 slower than 16) and slow the API too, because everything shares one event loop. The documented next step is workers in separate processes, not tested here. Backpressure is a growing queue: nothing is rejected, and end-to-end latency in a burst is queue wait.

**Honesty rules for the numbers.** Single runs, not averages; the first run's p99 includes warm-up and says so; load generator and server share the machine; no real LLM latency or network latency to the databases; `docs/INTERVIEW.md` quotes only numbers that are in `docs/benchmarks.md` and says "on my laptop". The one `consumer group missing; recreating` log line is the engine repairing itself after Redis was wiped between runs.

**README.** Rewritten around: pitch, live link (placeholder until deploy), Mermaid architecture, delivery guarantee (at-least-once delivery, one stored result per job, not exactly-once), how to run, benchmark table, design decisions, free-tier limits. The demo GIF is a TODO: it needs a screen recording of the deployed site, which cannot be made from here.

## Step 10b: PLANNER setting (fixed by default)
**Why `fixed` is the public default.** The public demo should need no key, spend no quota, and look the same every visit. With `PLANNER=fixed` a goal is created from a preset id (`POST /goals {"preset": "happy-path"}`, the same four ids as the recorded traces) and the planner job returns a built-in task graph from `app/planner.py`: no Gemini client is even built, `GEMINI_API_KEY` is ignored, and worker steps use the mock provider. A free-text goal is refused with a 400 that lists the presets, before the rate limiter is touched and before any row is written.

**`gemini` stays built and tested.** `PLANNER=gemini` is the earlier behaviour unchanged: Pydantic validation, one retry, then the fallback plan, and the mock planner when there is no key or the daily cap is used up. The test suite runs with `gemini` by default (an autouse fixture in `tests/conftest.py`), because most tests submit free-text goals; the fixed planner has its own tests.

**The page asks, it does not assume.** `GET /config` returns `{planner, presets}` (nothing secret). The free-text box is shown only when it says `gemini`; presets always show. If `/config` cannot be reached the box stays hidden, which is the safe side.

**Plan source.** A preset plan is stored with `plan_source = 'fixed'`. A planner job with no preset (queued before the switch) gets the deterministic mock plan, never an LLM.

## Step 11: the 3D "build the town" view
**Why three.js, and a real 3D scene.** A perspective camera, lights and shadows give the depth the 2D map cannot; the 2D PixiJS map stays as the fallback (no WebGL2, reduced motion, a failed chunk). three.js is a separate lazily loaded chunk, so first paint does not pay for it.

**Why the scene reads only the event store.** Recorded traces and the live WebSocket already go through one store (deduped by id, ordered), so feeding the 3D view from it means a recording and a live run can never look different, and nothing animates unless an event said so.

**Zones are DAG stages.** Tasks carry no kind or group, and adding one would mean changing the engine and re-recording the traces. A task's stage (1 + the deepest stage among its dependencies) is already in `task.created.depends_on`, works for every recorded trace, and gives the plan a natural left-to-right-around-the-ring reading. More than 6 stages merge into the last zone.

**The layout is data.** `docs/reference/town-layout.json` is written by a seeded script and read by the app and its tests: zones, 8 slots each, roads as spline centrelines, the path graph, barriers and props. Tests check no overlaps, every slot reachable from the plaza, one non-crossing main ring, props off the roads, the decoration triangle budget, and that every preset and recorded trace fits.

**Static geometry is merged, repeated things are instanced.** Roads, plaza, foundations, robots and lamps are one mesh with vertex colours; trees, bushes, rocks, flowers, fences and road arrows are InstancedMeshes. Phase A draws in 40 calls and 14k triangles against budgets of 150 and 56k.

**The camera fits the board, not a fixed number.** The default distance is found by projecting the board's corners through the real camera so the widest part takes 85% of the width; it is recomputed on resize and the zoom limits are relative to it.

