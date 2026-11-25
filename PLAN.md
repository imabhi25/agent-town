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
- [x] Integration tests against real Postgres + Redis: kill-worker, crash between commit and XACK, duplicate submit, DLQ, retry (`tests/test_recovery.py`, `test_worker.py`, `test_retries.py`, `test_jobs_api.py`, `test_dlq_api.py`)
- [x] Synthetic load generator

## Week 3: visual and controls
- [x] Vite + React + TS scaffold and event store (dedupe, order, gap/reconnect refetch) in web/ (step 6)
- [x] PixiJS city map: Planner, Queue, Workers, Downstream, DLQ, Done, circuit gate (step 7)
- [ ] Panels: event log, task checklist, throughput, p99, queue depth (queue depth and counts are in the map summary; the rest is later)
- [x] Cap mock_latency and only accept mock_* fields from trusted input (payload fields can currently make a worker sleep arbitrarily long)
- [x] Chaos panel (kill worker, failure rate, flood), approval card, DLQ replay (step 7)
- [x] Prompt box and recorded/live modes: 4 recorded presets from the real engine, replay engine, health ping (step 8)
- [ ] Optional: isometric art with a free asset pack (+2-3 days)

## Week 4: ship
- [x] Deploy files and guide: Vercel + Render + Neon + Upstash, replay fallback (step 9; the manual account steps are in docs/DEPLOY.md)
- [x] Daily caps in code (Gemini calls, live goals), per-IP limits, Redis command budget guard (step 4)
- [ ] Link Google AI Pro Cloud credits; budget alert
- [x] k6 benchmark, architecture diagram, design notes, resume bullets (step 10); demo GIF recorded locally against the real engine with the mock planner (`web/scripts/record-demo.mjs`)
- [x] Stretch: Redis Lua token-bucket rate limiter, circuit breaker
- [x] `PLANNER=fixed|gemini` setting: presets only by default, no LLM or key needed (#17)
- [x] Chaos campaign: worker/dispatcher processes and Redis killed with SIGKILL, Postgres-only checker, deliberately broken XACK-before-commit mode it must catch, README numbers generated from `results/chaos/campaign.jsonl`, small campaign in CI (#26)
- [x] Engine benchmark with worker processes: throughput and p50/p95/p99 per worker count, recovery time after kill -9, README tables generated from `results/bench/engine.jsonl`; README rewritten to lead with the guarantee and numbers

## MVP done when
Live URL; a prompt produces a plan and agents visibly work; kill-worker test passes in CI; benchmark numbers in README; demo works without Gemini quota (mock fallback).

## Step 11: "build the town" 3D view (three.js)
Status: Phase A and Phase B slices 1 to 3 are merged (#18). Still open: the Foreman turning toward the
latest event, snap-when-behind for 4x replay, Playwright e2e, and Phase C (performance, fallbacks, docs). The design brief file (`docs/reference/design-brief.md`) never arrived, so
this follows the written change request and the concept images; where a value was not given (walking
speed and so on) the plan says so and it is easy to change.

### The metaphor
The town starts nearly empty and is BUILT as a goal runs: every task of the current goal is a
building slot in the zone for its part of the plan; a finished job raises the building. Workers are
small markers walking between the plaza and the zones; a Foreman robot stands on the plaza. The old
2D PixiJS map stays as the fallback (no WebGL2, reduced motion, chunk failure) and the 3D view reads
the same event store, so recorded traces and the live WebSocket draw the identical scene.

### What the event stream really carries (checked against `app/` and the recorded traces)
- A **task** has `key` (t1..), `name`, `depends_on`, `needs_approval` (event `task.created`, and the
  snapshot's `tasks`). It has **no kind or group**, so a zone is the **DAG stage**: 1 + the deepest
  stage among its dependencies. No new field, no engine change, and the existing recorded traces
  work (they are all the 4-task diamond: stages of 1, 2 and 1 tasks, so 3 zones). Plans with more
  than 6 stages merge the extra stages into the last zone.
- A **job** event (`job.started`, `.succeeded`, `.retry_scheduled`, `.dead`, `.replayed`,
  `.recovered`) carries `job_id` and `worker`, not the task. The link is `task.queued {job_id,
  task_id}`. Jobs with no task (the planner's own job, flood jobs) go to the Foreman, not a zone.
- `job.retry_scheduled` carries `delay` (seconds) and `attempt`, so the retry **countdown can be
  shown** (it counts down in real time, so it is only exact at 1x replay).
- `circuit.opened/closed` carry `provider` ("llm" or "planner"), not a zone. Design choice: one
  barrier on the spoke into every zone, all lowered by the `llm` circuit.
- `goal.started` / `goal.succeeded` carry timestamps (`ts`), which give the elapsed time for the
  completion banner. There is no "check/record/handoff" event, so none is invented.
- A snapshot's `workers` have `job_id` but the snapshot has no job-to-task link, so a job already
  running when the page connects cannot be placed in a zone until its next event.
- The planner's `fixed` templates (PLANNER=fixed, PR #17) already give every preset a valid DAG: the
  stage sizes are happy-path 1,2,1; retries 1,2,1,1; chaos-recovery 1,3,1; dlq-replay-approval
  1,1,1,1. `tests/test_town_layout.py` checks all of them, the recorded traces and the worst cases
  (8 tasks in a row, 8 in one stage) against the layout.

### Layout (40 x 30 m board, data in `docs/reference/town-layout.json`)
Generated by `scripts/gen_town_layout.py` from a fixed seed (a test checks the committed file is what
the script writes). Top-down, north at the top; `1`..`6` are the zones (stage order), `F` the
Foreman on the plaza, `=` the ring road (arrows run 1 to 2 to ... 6 and back), `-` spokes from the
plaza to each zone, `~` the plaza ring, `#` the six barriers, `o` the edge of a zone.

```

                                          ooooooooo
                     ooo               ooo         ooo
                 oooo   oooo          oo             oo
               oo           oo       oo               oo
              o               o      o                 o         ooo
             o                 o     o        3        o     oooo   oooo
             o        2        o     oo               oo   oo           oo
             o                 o      oo             oo   o               o
              o               o  ===   ooo         ooo   o                 o
               oo           oo==== =======ooooooooo      o        4        o
                 oooo   oooo===-          ============== o                 o
          ooooooooo  ooo  ===  -     ~~~~~-#-    ------== o               o
       ooo         ooo  ===    --#---~     ~~~--#       == oo           oo
      oo             oo  =        ~~         --          =   oooo   oooo
     oo               oo =-----#---     F     ~          ==      ooo
     o        1        o =-       ~~         ~~           ==
     o                 o ==        ~~~     ~~--           ==
     oo               oo   ======    ~-#~~~~  -#         ==    ooo
      oo             oo         =======-       ----     == oooo   oooo
       ooo         ooo        ooooooooo=======    ----===oo           oo
          ooooooooo        ooo         ooo    ==== ==== o               o
                          oo             oo       ==   o                 o
                         oo               oo           o        5        o
                         o        6        o           o                 o
                         o                 o            o               o
                         oo               oo             oo           oo
                          oo             oo                oooo   oooo
                           ooo         ooo                     ooo
                              ooooooooo
```
- **6 zone sites**, radius 4.6 m, 8 building slots each (a plan has at most 8 tasks). Slots sit on a
  3 m ring around the zone's signpost, offset so the entrance footpath runs between two of them;
  position and rotation are jittered from the seed.
- **Plaza** at the centre (radius 3.4): the Foreman on a podium and 8 idle pads for workers.
- **Roads**: a closed **ring** through the six zone gates in stage order (the one main route, with
  painted arrows), a **spoke** from the plaza to each gate, a short ring road round the plaza, and
  footpaths (zone gate to signpost, signpost to every slot, pads to the plaza). All are Catmull-Rom
  splines turned into flat ribbons. The path graph has 75 nodes and 81 edges.
- **Per preset** (zones used, slots used): happy-path 3 zones (1, 2, 1); retries 4 (1, 2, 1, 1);
  chaos-recovery 3 (1, 3, 1); dlq-replay-approval 4 (1, 1, 1, 1). Unused zones stay bare grass.
- **Decoration**: instanced trees, bushes, rocks, flowers and fence segments (under the 8,000
  triangle budget, checked by a test), merged lamps; none on a road, a slot area, the plaza or a barrier.

### Phase A as built
`city3d/town.ts` (layout types, spline sampling, shortest paths over the graph),
`city3d/kit.ts` (merge-by-vertex-colour batches, ribbons, board slab), `city3d/TownStatic.tsx` (the
static town), `city3d/camera.ts` (fit the board to 85% of the viewport width through the real
camera, zoom limits relative to that), `city3d/Scene3D.tsx` (45 degree camera, FOV 35, ACES,
hemisphere + sun, 2048 soft shadows). The static scene previews the happy-path preset: three zones
labelled with a step badge and `0/M built`, ghost foundations, three idle workers and the Foreman.
Measured: 40 draw calls, 14,074 triangles.

### Phase B (after approval): building states per task (planned ghost, running scaffold, retrying
orange with the countdown, succeeded rise, DLQ rubble with a red beacon, replay), event-driven
worker markers along the path graph with snap-when-behind, barriers, the Foreman turning toward the
latest event, the completion sequence, the pipeline panel and legend, event-store tests (duplicates
and out-of-order events).



### Phase B progress
Slice 1 (done): `city3d/townModel.ts` folds the store's events into a `TownModel` (tasks with zone, slot,
status; workers; llm circuit; goal timing), `walker.ts` walks workers along the road graph,
`TownLive.tsx` draws buildings per status (ghost, scaffold, orange retry with countdown, raised, DLQ
rubble with a red beacon, approval label), robots and zone counters, and the barriers swing with the
circuit. `Scene3D` takes the model; with no model it still shows the Phase A preview.
Correction to the notes above: `task.created.depends_on` holds task **ids**, not keys (checked against
the recorded traces), so stages are computed over ids.
Not done yet: Foreman turning toward the latest event (the Foreman is still baked into the static
batch), snap-when-behind for replay at 4x (walkers only hurry), Playwright e2e.

Slice 2 (done): in 3D a headless `Scene` (same event rules as 2D) feeds the DOM panels, so approval
cards, dead letters, the worker list and the legend work in both views; clicking a robot kills that
worker (refused while a recording plays, as in 2D). Bug found and fixed on the way (it also hit 2D):
restarting a recording replayed the same event ids, which the scene skipped as already applied. The
store now has an `epoch` that `reset()` bumps, and `Scene.sync` starts over when it changes.

Slice 3 (done): `PipelinePanel` (DOM, under the map in 3D) lists each stage with its tasks and state,
and shows a completion banner "Town built in 10.4s: 4 tasks, 7 retries" from `goal.started` and
`goal.succeeded` timestamps; a "Town built!" flag appears over the Foreman. The model now counts
retries. No celebration animation: it would not come from any event, so it was left out.
