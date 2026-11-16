# Interview notes

Every number below is from `docs/benchmarks.md` (local laptop, single process, mock LLM at about
125 ms per call). Say "on my laptop" when you quote them.

## Resume bullets (pick 3 to 4)
1. Built a distributed job engine (FastAPI, asyncio, Redis Streams, PostgreSQL) with a transactional
   outbox, retries with exponential backoff and jitter, a dead-letter queue with replay, and
   heartbeat/`XAUTOCLAIM` crash recovery; sustained 600 job submissions/s with a 1.4 ms p95 and no
   errors, and drained 2,000 queued jobs at about 100 jobs/s from a single Python process.
2. Designed for at-least-once delivery with idempotent completion (one Postgres transaction, guarded
   update, unique output per job, then XACK): in 3 chaos runs of 500 jobs with 30 to 35 worker kills
   each and 10% provider failures, all 1,500 jobs completed with exactly one stored result and one
   success event each and zero pending messages.
3. Added production protection for a free-tier public demo: Redis Lua token-bucket rate limiting,
   per-provider circuit breaker, daily caps on LLM calls and live goals, a Redis command-budget guard,
   an idle sleep mode, and startup config checks that refuse to run with an unsafe CORS setup.
4. Built a real-time PixiJS city map driven only by the backend's event stream over WebSocket
   (deduplicated by id, ordered by time, refetch on gaps), plus recorded traces captured from the real
   engine so the site works instantly with no backend; 103 unit and 19 browser tests and CI on every PR.

## 60-second walkthrough
"Agent Town is a live map of a real job engine. You type a goal, a planner turns it into a graph of
tasks, and workers pick them up through Redis Streams while the map shows every event as it happens.

The core problem is not losing or duplicating work. A job and its outbox row are written in one
Postgres transaction, a dispatcher publishes them to a stream, and workers complete a job in one
transaction before they acknowledge. Delivery is at-least-once, so completion is idempotent:
I do not claim exactly-once. To check that, I ran 500 jobs with 4 workers, killed a worker every
few tenths of a second, and made 10% of calls fail. Across three runs about 30 workers were killed
each time, and every one of the 1,500 jobs finished with exactly one stored result.

On my laptop one Python process accepts 600 jobs a second with a 1.4 millisecond p95, and with 8
workers it processes about 60 jobs a second, which is 96% of what 8 workers could do at that call
latency. Past about 16 workers it tops out near 100 jobs a second because everything shares one event loop. I
measured that, and the fix would be moving workers into separate processes.

The public demo runs on free tiers, so it starts on recorded traces from the real engine, wakes the
backend in the background, and switches to live when it answers. Rate limits, a circuit breaker and
daily caps keep it inside the free limits."

## Questions to be ready for
- *Why not exactly-once?* Redis Streams redelivers unacknowledged messages, and a crash can happen
  between commit and XACK. The guarded update makes the second run a no-op. That is effectively-once
  for our state; the external call may repeat.
- *Where is the ceiling and why?* One event loop: API, publisher and workers share it, and each
  completion is a Postgres transaction. 4 and 8 workers reach about 96% of ideal; 16 reach 78%; 32 get worse.
- *What would you change for real scale?* Workers as separate processes or services, batching event
  inserts, partitioning the events table, and measuring against a hosted database.
- *What were the bugs the tests found?* A worker pool crash after every worker was killed; queue count
  drifting because `POST /jobs` emitted no event; and, booting the live profile in a clean
  environment, redis-py 8's 5 s default socket timeout colliding with a 5 s blocking read.
- *What are the limits of the benchmark?* Same machine for load generator and server, mock LLM,
  single runs, local databases (see "What these numbers do not show").
