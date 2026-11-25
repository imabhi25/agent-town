# Benchmarks

> This page is the original single-process k6 benchmark of the HTTP API and embedded workers.
> The multi-process engine benchmark (throughput, p50/p99 and recovery time after kill -9) is
> generated into the README from `results/bench/engine.jsonl` by `scripts/bench_report.py`.

Everything here is a single run (one run per row) on one Apple M3 laptop against the LOCAL stack, with
k6 on the same machine and a mock LLM at about 125 ms per call. These are not free-tier numbers.
The free tiers (a shared Render CPU, Neon, Upstash over the internet) are slower and capped, so do
not read these as numbers for the hosted demo.

## Setup
- **Machine:** Apple M3, 8 cores, 8 GB RAM, macOS 27.
- **Stack:** one `uvicorn` process (API + dispatcher + event publisher + workers in-process,
  `EMBEDDED_WORKERS=true`), PostgreSQL 18 and Redis on the same machine (no network latency), mock
  LLM provider with 50 to 200 ms latency per call (mean about 125 ms), no failures injected.
- **Load generator:** k6 v2.4.0 (`bench/jobs.js`) on the same machine, so it competes with the
  server for CPU. Every request is `POST /jobs` with a unique `Idempotency-Key`.
- **Reproduce:** `bench/run.sh rate <workers> <req/s> <duration>` and `bench/run.sh burst <workers> <jobs>`
  (wipes the scratch database and Redis DB it points at). Raw results: `bench/results.jsonl`.
- One run per row. These are single measurements, not averages: expect a few percent of noise, and
  more on the p99 column.

## 1. Accepting jobs (API ingest)
Constant arrival rate for 10 s, 8 workers running at the same time. A job is accepted when its
row and outbox row are committed in one transaction.

| Offered rate | Achieved | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|
| 100 req/s | 100 req/s | 1.6 ms | 20.3 ms | 288.5 ms | 0 |
| 300 req/s | 300 req/s | 0.9 ms | 1.6 ms | 3.9 ms | 0 |
| 600 req/s | 600 req/s | 0.6 ms | 1.4 ms | 5.5 ms | 0 |

The 100 req/s run was the first after the server started, so its p95 and p99 include warm-up (new
database connections and a cold cache); the later rows are the steady state.

Peak: 2,000 jobs sent as fast as 50 virtual users can (`burst`), 4 workers running:
**2,395 req/s, p50 18.8 ms, p95 50.6 ms, p99 66.2 ms, 0 errors.**

## 2. Processing jobs (engine throughput)
2,000 jobs are submitted in one burst, then the time from the first job created to the last job
completed is measured. "Ideal" is `workers / 0.125 s`, what perfectly parallel workers could do
with this mock latency.

| Workers | Ideal jobs/s | Measured jobs/s | Drain time | Succeeded | Dead |
|---|---|---|---|---|---|
| 4 | 32 | 30.6 | 65.3 s | 2000 | 0 |
| 8 | 64 | 61.5 | 32.5 s | 2000 | 0 |
| 16 | 128 | 100.0 | 20.0 s | 2000 | 0 |
| 32 | 256 | 87.3 | 22.9 s | 2000 | 0 |

How to read it: up to 8 workers the engine is worker-bound and scales almost linearly (about 96%
of ideal). Past that it hits the ceiling of ONE Python process, about 100 jobs/s here: the workers
share one event loop with the API and the event publisher, and each completion is a Postgres
transaction plus an event row. At 32 workers it is slightly slower than at 16 (more tasks fighting
for the same loop), and the API gets slower too: ingest in the same burst fell from 2,395 req/s with 4
workers to 1,290 with 16 and 499 with 32 (p95 from 51 ms to 283 ms). The way past the ceiling is
more processes (workers outside the API, `python -m app.worker`), not more tasks in one.

End-to-end latency (job created to completed) in a burst is mostly queue wait, because 2,000 jobs
arrive at once. With 8 workers the p50 was 16.0 s, p95 30.1 s, p99 31.4 s. This is the time a job
spends behind the others, not the cost of one job (about 125 ms). For the 600 req/s ingest run,
which offered about 10 times the 8-worker capacity, the queue grew for the whole run and the
end-to-end p95 was 82 s. Backpressure here is a growing queue, by design: nothing was rejected or
lost.

## 3. Correctness under chaos
`scripts/demo_chaos.py`: 500 jobs, 4 workers, 10% of provider calls fail (429/500), and every 0.15
to 0.4 s a random worker (preferably one in the middle of a job) is killed and replaced. Three runs
(seeds 1, 2, 3):

| Seed | Workers killed | Jobs taken over from a dead worker | Retries | Succeeded | `job_outputs` rows | `job.succeeded` events per job | XPENDING | Duration |
|---|---|---|---|---|---|---|---|---|
| 1 | 30 | 28 | 58 | 500/500 | 500 | exactly 1 | 0 | 9.9 s |
| 2 | 35 | 29 | 47 | 500/500 | 500 | exactly 1 | 0 | 10.7 s |
| 3 | 30 | 27 | 50 | 500/500 | 500 | exactly 1 | 0 | 8.3 s |

Delivery is at-least-once (a taken-over job can run its provider call again); completion is one
transaction guarded by `WHERE status != 'succeeded'`, so the result is recorded once: at-least-once delivery plus
idempotent completion gives one stored result per job (delivery is not exactly-once). In every benchmark run above, `job_outputs` had one row per job and there was one
`job.succeeded` event per succeeded job.

## What these numbers do not show
- Real LLM latency (the mock is 50 to 200 ms; Gemini is slower and rate-limited), so real
  throughput is bounded by the provider, not by the engine.
- Network latency to a hosted Postgres or Redis. Each job costs several round trips to each, so a
  hosted database will lower jobs/s.
- Multi-process scaling: only a single process was tested here. Worker processes are measured
  by `scripts/bench_engine.py` (results in `results/bench/engine.jsonl`, table in the README).
- The log line `consumer group missing; recreating` appeared once during the matrix: it is the
  engine repairing itself when Redis was wiped between runs, not an error.
