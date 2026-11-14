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
