# Agent Town

A live city map of a real distributed job engine. Type a goal, a Gemini planner breaks it into tasks, and workers ("agents") process them through Redis Streams with retries, a dead-letter queue, crash recovery and human approval gates. Every move on the map is a real event from the backend.

> Status: week 1 (core engine) complete. See `PLAN.md`.

## Stack
FastAPI + asyncio, Redis Streams, PostgreSQL, Gemini (`google-genai`), React + TypeScript + PixiJS (week 3), Docker Compose, pytest, GitHub Actions.

## Run locally
```bash
cp .env.example .env
docker compose up -d postgres redis
pip install -e ".[dev]"
uvicorn app.main:app --reload
pytest
```

## Architecture
```
POST /jobs ──▶ Postgres: jobs + outbox row (ONE transaction)
                  │
            dispatcher (polls outbox, XADD, then marks published)
                  ▼
            Redis Stream "jobs" ──▶ consumer group "workers" (N asyncio workers)
                                         │  claim (row lock) ▶ provider call (timeout) ▶ complete (ONE transaction) ▶ XACK
                  ┌──────────────────────┼───────────────────────────┐
         retryable error            non-retryable error /        worker dies
     (429/500/timeout)               max attempts reached      (no XACK, heartbeat stops)
                  ▼                          ▼                           ▼
   Redis sorted set "jobs:delayed"      Postgres status='dead'     XAUTOCLAIM after the lease;
   (backoff + full jitter) ─ mover ▶    + Redis Stream "dlq"       a live worker takes the job over
   back onto the stream                 (replay via outbox)        if the owner's heartbeat is gone
```

1. **Outbox.** The API writes the job and an outbox row in one transaction, so a job is never lost between the database and the queue.
2. **Dispatcher.** Publishes outbox rows to the `jobs` stream (`XADD`, then marks them published). A crash in between causes a duplicate message, never a lost one.
3. **Stream and workers.** Workers read through one consumer group. Each provider call runs under `asyncio.wait_for` (`PROVIDER_TIMEOUT`, default 30s), so a hung call becomes a retryable timeout.
4. **Retries and DLQ.** Retryable errors wait in a Redis sorted set (exponential backoff with full jitter), so a delay never holds a worker. After `max_attempts`, or on a non-retryable error, the job is marked `dead` and added to the `dlq` stream. `replay_dlq` re-enqueues it through the outbox.
5. **Recovery.** Every worker has a heartbeat key with a TTL. A message pending longer than the lease is taken over with `XAUTOCLAIM`, and the job is run again only if its previous owner has no heartbeat. Settings are validated at startup: `heartbeat_ttl` must be less than `lease_seconds`.

Design reasoning for every step is in `DECISIONS.md`.

## Delivery guarantee
Redis Streams redelivers unacknowledged messages (XAUTOCLAIM), so a job can run more than once. The external call (LLM) has no side effects in our system, so repeating it is safe. Completion is one Postgres transaction: update the job `WHERE status != 'succeeded'`, insert the output (unique on job_id), check the affected row count, commit, then XACK. A crash between commit and XACK causes redelivery, the guarded update matches nothing, and nothing is duplicated. Result: effectively-once. We never claim exactly-once delivery.

## Chaos demo
`app/chaos.py` submits jobs, then repeatedly kills a random worker (preferring one that is mid-job) and starts a replacement while the jobs run. `tests/test_chaos.py` runs it in CI with 200 jobs and 4 workers and asserts: every job `succeeded`, exactly one `job_outputs` row and one `job.succeeded` event per job, and `XPENDING` is 0.

Run the same scenario against the compose Postgres and Redis:
```bash
python scripts/demo_chaos.py --up --reset
```
Example output (200 jobs, 4 workers, 10% mock provider errors):
```
jobs submitted      200
jobs succeeded      200
jobs dead (DLQ)     0
workers killed      13
recoveries          10   (dead worker's job taken over)
retries             22   (provider errors, backoff)
extra attempts      32
job_outputs rows    200   (one per job: True)
job.succeeded       200 jobs   (exactly one each: True)
XPENDING            0
duration            3.9s

RESULT: PASS (nothing lost, nothing duplicated)
```
`--reset` truncates the job tables and deletes the job streams first (the script refuses to run on a non-empty jobs table without it). Options: `--jobs`, `--workers`, `--failure-rate`, `--seed`.
