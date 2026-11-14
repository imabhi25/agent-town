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
4. On a provider error (429/500/timeout) the job is marked `failed`, a `job.failed` event is written, and the message is XACKed. Day 5 replaces this with retries and a DLQ.

Crash behaviour: `kill_worker` cancels the task. Only `ProviderError` and `TimeoutError` are caught, `CancelledError` is not, and no XACK is in a `finally`. A killed worker leaves the job `running` and the message pending, which is what the Day 6 XAUTOCLAIM recovery picks up.

Known limit: the claim only guards against `succeeded`. If the same job is delivered to two workers while the first is still `running`, both call the provider (at-least-once). Completion is still single-effect because the guarded UPDATE lets only one writer insert the output and the `job.succeeded` event. Day 6 (heartbeats, claim ownership) narrows this window.
