# 4-week plan

## Week 1: core engine (no UI)
- [x] Day 1: repo, Docker Compose, FastAPI skeleton, CI
- [x] Day 2: schema; POST /goals stub and POST /jobs with Idempotency-Key (+ outbox row)
- [x] Day 3: dispatcher moving outbox rows into a Redis Stream (tested against real Postgres + Redis)
- [x] Day 4: worker pool (consumer group), mock LLM (latency, 429/500/timeouts)
- [x] Day 5: retries (exponential backoff + full jitter), max attempts, DLQ stream
- [x] Day 6: XAUTOCLAIM recovery, heartbeats, transactional completion
- [x] Day 7: buffer. Done when a worker killed mid-job still completes the job once in effect.

## Week 2: planner, dependencies, tests, events
- [x] Gemini planner (JSON via response_schema, Pydantic validation, retry once, fallback plan)
- [x] Task dependencies: enqueue only when dependencies finish
- [x] Events stream + WebSocket gateway (batched ~15 updates/sec)
- [ ] Integration tests with testcontainers: kill-worker, crash between commit and XACK, duplicate submit, DLQ, retry
- [x] Synthetic load generator

## Week 3: visual and controls
- [x] Vite + React + TS scaffold and event store (dedupe, order, gap/reconnect refetch) in web/ (step 6)
- [ ] React + TypeScript + PixiJS city map (queue, workers, DLQ, downstream)
- [ ] Panels: event log, task checklist, throughput, p99, queue depth
- [x] Cap mock_latency and only accept mock_* fields from trusted input (payload fields can currently make a worker sleep arbitrarily long)
- [ ] Chaos panel (kill worker, failure rate, flood with mock jobs), approval gate, DLQ replay, chat panel
- [ ] Optional: isometric art with a free asset pack (+2-3 days)

## Week 4: ship
- [ ] Deploy (Fly.io or Railway + Vercel), replay fallback
- [x] Daily caps in code (Gemini calls, live goals), per-IP limits, Redis command budget guard (step 4)
- [ ] Link Google AI Pro Cloud credits; budget alert
- [ ] k6 benchmark in README, architecture diagram, design notes, demo GIF, resume bullets
- [x] Stretch: Redis Lua token-bucket rate limiter, circuit breaker

## MVP done when
Live URL; a prompt produces a plan and agents visibly work; kill-worker test passes in CI; benchmark numbers in README; demo works without Gemini quota (mock fallback).
