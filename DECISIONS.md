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
- Heartbeats prove the process is alive, not that the job is making progress. A hung provider call keeps its job locked. A provider call timeout is future work.
- If a worker is only paused (GC, network blip) past the heartbeat TTL, a second worker can take over and both may call the provider. Effective-once still holds because only one completion transaction matches.
- Reclaiming reads at most 10 messages per cycle. Stale consumers are not removed from the group (no XGROUP DELCONSUMER yet).
