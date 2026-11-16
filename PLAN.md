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
- [x] k6 benchmark, architecture diagram, design notes, resume bullets (step 10); demo GIF is a placeholder (needs a recording of the deployed site)
- [x] Stretch: Redis Lua token-bucket rate limiter, circuit breaker

## MVP done when
Live URL; a prompt produces a plan and agents visibly work; kill-worker test passes in CI; benchmark numbers in README; demo works without Gemini quota (mock fallback).

## Step 11: true 3D city view (three.js)
Status: Phase A (static scene) approved. Phase B (behaviour) built; Phase C (performance, tests, ship)
starts only after approval.

### What the two references show (docs/reference/ref-1.png, ref-2.png)
Both are phone screenshots of the same prototype (a town where AI sub-agents work). They are kept
locally and not committed: they are someone else's post.
- **ref-1** is a close crop of one district: a low-poly town on a pale ground with a faint dot
  grid. Chunky two-storey houses with flat tiled roofs, a row of houses with golden onion domes,
  round pastel trees (teal, green, sage) as spheres on short trunks, and thin white outline boxes
  for plots not built yet. Tiny yellow figures stand beside buildings. Each figure has a small
  blue name tag, and buildings have white callout labels ("Approval cards", "Event log").
- **ref-2** is the whole town on a floating slab with a grey rim. The slab is split into pastel
  districts (lavender, mint, butter yellow, peach, sky) separated by paved paths, with a coloured
  signpost per district, trees scattered along the edges, and the UI (chat left, checklist right)
  floating around it on a light dot-grid page.
- **Copied in spirit:** a floating pastel slab, one colour-tinted district per building (our
  existing state colours), simple chunky low-poly shapes, one gold dome, spherical trees, white
  rounded callout labels, soft light and soft shadows, a camera looking down at roughly 40 degrees.
- **Not copied:** the content, the character design (ours are small robots) and the layout. The
  references are drawn isometric; ours is a real perspective camera, because the brief asks for a
  true 3D scene.

### Choices
- **react-three-fiber + drei** (web/ is React 18, so r3f 8 and drei 9). The scene is declarative,
  disposal on unmount is handled by r3f, and `useFrame` gives a render-loop hook for Phase C.
  Only `OrbitControls` and `Html` are taken from drei, to keep the bundle small.
- **Lazy loading:** `Scene3D` is loaded with `React.lazy` + dynamic `import()`, so three.js is a
  separate chunk that is fetched only when the 3D view is shown. First paint of the page is the
  same as before.
- **Fallback:** the 2D PixiJS map stays untouched. Default view is 3D only when WebGL2 works and
  `prefers-reduced-motion` is off; otherwise 2D. A 3D/2D toggle switches at any time. `?view=2d|3d`
  forces a view (used by screenshots and tests). Phase C adds context-loss handling.

### File layout (all under web/)
- `src/city3d/Scene3D.tsx` the Canvas, lights, camera, controls (lazy entry point)
- `src/city3d/layout.ts` world positions of the six buildings, roads and trees, converted from the
  2D `BUILDINGS` table so both views share one layout
- `src/city3d/Buildings.tsx`, `Props.tsx` (trees, robots, slab, roads, labels)
- `src/city3d/webgl.ts` WebGL2 + reduced-motion detection and the default-view rule (unit tested)
- `src/city3d/palette3d.ts` the existing PALETTE as three.js colours
- `public/models/` (compressed GLBs, if any) and `assets/` credits

### Assets
- Planned: Kenney City Kit Commercial / Industrial / Roads and the Quaternius Animated LowPoly
  Robot (all CC0), compressed with gltf-transform to stay under 5 MB in total.
- Problem found: kenney.nl answers scripted downloads with 403 and Quaternius serves its packs
  from a Google Drive folder, so they cannot be fetched from here. Phase A therefore builds
  everything (buildings, trees, robots, roads) from three.js primitives, which the brief allows.
  Dropping the packs into `web/assets-src/` lets a later step swap models in.

### Event to scene mapping (Phase B, written now so the design is settled)
The existing headless `Scene` in `src/scene.ts` already turns store events into worker/job state
and is unit tested without WebGL. The 3D view will be a second renderer of that same `Scene`
(world x,y -> 3D x,z), so mapping logic is shared and nothing invents activity:
`job.started` worker walks Queue -> Downstream, `job.succeeded` green and on to Done,
`job.retry_scheduled` orange and back to the Queue, `job.dead` red to the DLQ, `worker.killed`
fade out, `worker.restarted` a new robot appears in the yard, `circuit.opened/closed` gate bar
down/up, `task.awaiting_approval` a gate object that holds until approved.
Phase A is static: four idle robots in the Workers building, no animation, no event input.

### Phase B as built
- `city3d/Town3D.tsx` owns a headless `Scene` (the same class the 2D map uses) and feeds it the
  store's snapshot and events each frame, so 3D and 2D cannot disagree about what happened.
- `city3d/poses.ts` (pure, no three.js) maps each Scene worker onto 3D roads: when the Scene gives a
  worker a new destination, a route is built over the road graph in `city3d/routes.ts` (through the
  circuit gate on the way to Downstream, around the Downstream building on the way to Done), and
  the robot sits at the same fraction of that route as the Scene has walked of its straight line.
  Arrival and state changes therefore stay exactly in step with the Scene.
- Robots carry a job cube whenever they hold a job; a cube flies from the Queue to the worker on
  `job.started`; queue, done and DLQ counts show as instanced token piles plus counters.
- State shows on the model: one accent material per robot (visor, cap, chest light, bulb, carried
  cube) is idle grey, busy blue, retrying orange, failed red, success green.
- Circuit gate: a boom barrier that lowers (red) on `circuit.opened`, lifts (green) on `circuit.closed`.
- Approval gate: an arch across the road into Done, shown only while a step awaits approval.
- Click a robot: `onWorkerTap` kills it through the same API call as the 2D map (live mode only).
