# Progress log

| Step | PR | CI | Tests | Notes |
|------|----|----|-------|-------|
| Prereq: Days 4-6 | [#1](https://github.com/imabhi25/agent-town/pull/1) | pass | 47 | Squash-merged so main has the worker code |
| Prereq: Day 7 | [#3](https://github.com/imabhi25/agent-town/pull/3) | pass | 50 | Rebased onto main (#2 closed: squash of #1 made it conflict) |
| 1. Gemini planner | [#4](https://github.com/imabhi25/agent-town/pull/4) | pass | 76 | Planner job, validation, mock + cap |
| 2. Run a goal end to end | [#5](https://github.com/imabhi25/agent-town/pull/5) | pass | 85 | Dependencies, approvals, goal failure |
| 3. Live events and WebSocket | [#6](https://github.com/imabhi25/agent-town/pull/6) | pass | 97 | Events outbox -> stream, hub, /ws, /state |
| 4. Protection, limits, free-tier budget (+ /healthz) | [#8](https://github.com/imabhi25/agent-town/pull/8) | pass | 131 | Lua bucket, breaker, live profile, reconcile |
| 5. Chaos API, load generator, stats | [#9](https://github.com/imabhi25/agent-town/pull/9) | pass | 147 | /chaos/*, /stats, load generator |
| 6. Web scaffold and frontend event store | [#10](https://github.com/imabhi25/agent-town/pull/10) | pass | 152 py, 19 js | Dedupe, ordering, gap/reconnect refetch |
| 7. PixiJS city map, chaos panel, DLQ replay, approvals | [#11](https://github.com/imabhi25/agent-town/pull/11) | pass | 152 py, 55 js, 9 e2e | Event-driven scene, DOM controls |
| 8. Recorded presets and live mode | [#12](https://github.com/imabhi25/agent-town/pull/12) | pass | 163 py, 103 js, 19 e2e | Traces recorded from the real engine, replay, /healthz ping |
| 9. Free deployment files and guide | pending | pending | see PR | Dockerfile, render.yaml, vercel.json, migrations at startup, CORS lock, secret scan |
