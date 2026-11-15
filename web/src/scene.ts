// The scene is what the map shows: buildings, worker positions and colours, counters, the circuit
// gate. It changes ONLY in response to events from the store (see `sync`), never by inventing
// activity on a timer. `step(dt)` only moves things that an event already set in motion (walking,
// fading, easing the gate); nothing starts moving, fails or succeeds on its own.
//
// Written for the render loop: workers live in a fixed pool of objects that is reused, and
// `apply` / `step` allocate nothing per event except the id strings that arrive with it.
import type { WorkerStateName } from "./palette";
import type { Snapshot, TownEvent } from "./types";

export const WORLD_W = 960;
export const WORLD_H = 560;
export const MAX_WORKERS = 32;

export const CAPTIONS: Record<string, string> = {
  planner: "goal -> tasks",
  queue: "jobs wait here",
  yard: "click a worker to kill it",
  downstream: "LLM calls",
  done: "finished jobs",
  dlq: "gave up, needs a human",
};
const WALK_SPEED = 380; // world units per second
const FADE_SECONDS = 0.8;

export interface Building {
  id: "planner" | "queue" | "yard" | "downstream" | "done" | "dlq";
  label: string;
  x: number; // centre
  y: number;
  w: number;
  h: number;
}

export const BUILDINGS: Record<Building["id"], Building> = {
  planner: { id: "planner", label: "Planner", x: 130, y: 120, w: 150, h: 90 },
  queue: { id: "queue", label: "Queue", x: 130, y: 300, w: 150, h: 90 },
  yard: { id: "yard", label: "Workers", x: 450, y: 300, w: 300, h: 190 },
  downstream: { id: "downstream", label: "Downstream / LLM", x: 810, y: 200, w: 190, h: 100 },
  done: { id: "done", label: "Done", x: 810, y: 430, w: 150, h: 90 },
  dlq: { id: "dlq", label: "DLQ", x: 450, y: 485, w: 150, h: 90 },
};
// The circuit-breaker gate stands on the road between the yard and Downstream.
export const GATE = { x: 655, y: 230, w: 16, h: 70 };

export type Place = "queue" | "downstream" | "done" | "dlq";

export class SceneWorker {
  active = false;
  id = "";
  x = 0;
  y = 0;
  tx = 0;
  ty = 0;
  slot = 0;
  state: WorkerStateName = "idle";
  jobId: string | null = null;
  goingHome = false; // errand destination reached next? then walk back to the yard slot
  homing = false; // currently walking back to the yard slot
  dying = 0; // 1 -> 0 while a killed worker fades out
}

export interface DeadJob {
  jobId: string;
  type?: string;
  error?: string;
}
export interface PendingApproval {
  taskId: string;
  goalId: string;
  name: string;
}

export interface SceneSummary {
  workers: number;
  busy: number;
  queue: number;
  done: number;
  dead: number;
  circuit: "ok" | "tripped"; // breaker terms: tripped = circuit.opened (the gate swings shut)
  approvals: number;
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");

export function homeOf(slot: number): [number, number] {
  const y = BUILDINGS.yard;
  return [y.x - 105 + (slot % 8) * 30, y.y - 20 + Math.floor(slot / 8) * 34];
}

export function placeOf(place: Place, slot: number): [number, number] {
  const b = BUILDINGS[place];
  // Stand in front of the building, spread by slot so a crowd does not stack on one pixel.
  return [b.x + ((slot % 5) - 2) * 18, b.y + b.h / 2 + 16 + (Math.floor(slot / 5) % 3) * 12];
}

export class Scene {
  readonly pool: SceneWorker[] = Array.from({ length: MAX_WORKERS }, () => new SceneWorker());
  private byId = new Map<string, SceneWorker>();
  private applied = new Set<number>();
  private lastSnapshot: Snapshot | null = null;

  queueDepth = 0;
  done = 0;
  dead = 0;
  deadJobs: DeadJob[] = [];
  approvals: PendingApproval[] = [];
  openCircuits = new Set<string>();
  gate = 0; // 0 = open, 1 = closed (eased)
  plannerPulse = 0;
  queuePulse = 0;
  failureRate: number | null = null; // last known mock failure rate (null until an event says)
  version = 0; // bumped when something the DOM overlays show changes

  // ---- store -> scene ----------------------------------------------------------------------

  /** Bring the scene up to date with the store. Cheap when nothing changed. */
  sync(snapshot: Snapshot | null, events: TownEvent[]): void {
    if (snapshot !== null && snapshot !== this.lastSnapshot) this.rebase(snapshot);
    for (let i = 0; i < events.length; i++) {
      const e = events[i];
      if (this.applied.has(e.id)) continue;
      this.applied.add(e.id);
      this.apply(e);
    }
    if (this.applied.size > 4 * events.length + 1000) {
      this.applied = new Set(events.map((e) => e.id)); // keep it bounded
    }
  }

  /** A snapshot is the truth: reset counters and the worker list. Its events are already counted. */
  private rebase(s: Snapshot): void {
    this.lastSnapshot = s;
    for (const e of s.events) this.applied.add(e.id);
    this.queueDepth = s.queue.depth ?? 0;
    this.done = s.queue.succeeded ?? 0;
    this.dead = s.queue.dead ?? 0;
    this.deadJobs = (s.dead_jobs ?? []).map((d) => ({ jobId: d.job_id, type: d.type }));
    this.approvals = s.tasks
      .filter((t) => t.status === "awaiting_approval")
      .map((t) => ({ taskId: str(t.id), goalId: str(t.goal_id), name: str(t.name) }));
    const present = new Set<string>();
    for (const w of s.workers) {
      present.add(w.id);
      const sw = this.ensureWorker(w.id);
      if (sw && w.job_id) this.startJob(sw, w.job_id);
    }
    for (const sw of this.pool) if (sw.active && sw.dying === 0 && !present.has(sw.id)) this.release(sw);
    this.version++;
  }

  apply(e: TownEvent): void {
    const p = e.payload;
    switch (e.type) {
      case "goal.created":
        this.queueDepth++;
        this.plannerPulse = 1;
        break;
      case "goal.planned":
        this.plannerPulse = 1;
        break;
      case "job.created":
      case "task.queued":
      case "job.reconciled":
        this.queueDepth++;
        this.queuePulse = 1;
        break;
      case "chaos.flood":
        this.queueDepth += Number(p.count) || 0;
        this.queuePulse = 1;
        break;
      case "chaos.config": // someone (maybe another browser) changed the mock failure rate
        if (typeof p.failure_rate === "number") {
          this.failureRate = p.failure_rate;
          this.version++;
        }
        break;
      case "worker.started":
        this.ensureWorker(str(p.worker));
        break;
      case "worker.killed":
        this.kill(str(p.worker));
        break;
      case "job.started": {
        const w = this.ensureWorker(str(p.worker));
        if (this.queueDepth > 0) this.queueDepth--;
        if (w) this.startJob(w, str(p.job_id));
        break;
      }
      case "job.succeeded": {
        this.done++;
        this.errand(str(p.worker), "success", "done");
        break;
      }
      case "job.retry_scheduled":
        this.queueDepth++;
        this.queuePulse = 1;
        this.errand(str(p.worker), "retrying", "queue");
        break;
      case "job.dead": {
        this.dead++;
        this.errand(str(p.worker), "failed", "dlq");
        const jobId = str(p.job_id);
        if (!this.deadJobs.some((d) => d.jobId === jobId)) {
          this.deadJobs.push({ jobId, error: str(p.error) });
          if (this.deadJobs.length > 20) this.deadJobs.shift();
        }
        this.version++;
        break;
      }
      case "job.replayed": {
        if (this.dead > 0) this.dead--;
        this.queueDepth++;
        this.queuePulse = 1;
        const i = this.deadJobs.findIndex((d) => d.jobId === str(p.job_id));
        if (i >= 0) this.deadJobs.splice(i, 1);
        this.version++;
        break;
      }
      case "circuit.opened":
        this.openCircuits.add(str(p.provider));
        this.version++;
        break;
      case "circuit.closed":
        this.openCircuits.delete(str(p.provider));
        this.version++;
        break;
      case "task.awaiting_approval":
        if (!this.approvals.some((a) => a.taskId === str(p.task_id))) {
          this.approvals.push({ taskId: str(p.task_id), goalId: str(p.goal_id), name: str(p.name) });
        }
        this.version++;
        break;
      case "task.approved":
      case "task.rejected":
      case "task.cancelled":
      case "task.failed":
      case "task.succeeded": {
        const i = this.approvals.findIndex((a) => a.taskId === str(p.task_id));
        if (i >= 0) {
          this.approvals.splice(i, 1);
          this.version++;
        }
        break;
      }
    }
  }

  // ---- workers -----------------------------------------------------------------------------

  worker(id: string): SceneWorker | undefined {
    return this.byId.get(id);
  }

  private ensureWorker(id: string): SceneWorker | null {
    if (!id) return null;
    const existing = this.byId.get(id);
    if (existing) return existing;
    const used = new Set<number>();
    for (const w of this.pool) if (w.active) used.add(w.slot);
    const free = this.pool.find((w) => !w.active);
    if (!free) return null; // more workers than sprites: the extras are not drawn
    let slot = 0;
    while (used.has(slot)) slot++;
    const [hx, hy] = homeOf(slot);
    Object.assign(free, {
      active: true, id, slot, x: hx, y: hy, tx: hx, ty: hy,
      state: "idle", jobId: null, goingHome: false, homing: false, dying: 0,
    });
    this.byId.set(id, free);
    this.version++;
    return free;
  }

  private startJob(w: SceneWorker, jobId: string): void {
    w.state = "busy";
    w.jobId = jobId;
    w.goingHome = false;
    w.homing = false;
    [w.tx, w.ty] = placeOf("downstream", w.slot);
  }

  private errand(workerId: string, state: WorkerStateName, place: Place): void {
    const w = this.ensureWorker(workerId);
    if (!w) return;
    w.state = state;
    w.jobId = null;
    w.goingHome = true;
    w.homing = false;
    [w.tx, w.ty] = placeOf(place, w.slot);
  }

  private kill(id: string): void {
    const w = this.byId.get(id);
    if (!w) return;
    w.state = "failed";
    w.jobId = null;
    w.dying = 1;
    w.tx = w.x;
    w.ty = w.y;
    this.byId.delete(id); // the id is free for a replacement while the old sprite fades
    this.version++;
  }

  private release(w: SceneWorker): void {
    this.byId.delete(w.id);
    w.active = false;
    w.dying = 0;
    this.version++;
  }

  // ---- time --------------------------------------------------------------------------------

  /** Advance animations by dt seconds. Moves only what events already set in motion. */
  step(dt: number): void {
    const maxStep = WALK_SPEED * dt;
    for (let i = 0; i < this.pool.length; i++) {
      const w = this.pool[i];
      if (!w.active) continue;
      if (w.dying > 0) {
        w.dying -= dt / FADE_SECONDS;
        if (w.dying <= 0) this.release(w);
        continue;
      }
      const dx = w.tx - w.x;
      const dy = w.ty - w.y;
      const dist = Math.hypot(dx, dy);
      if (dist > maxStep) {
        w.x += (dx / dist) * maxStep;
        w.y += (dy / dist) * maxStep;
        continue;
      }
      w.x = w.tx;
      w.y = w.ty;
      if (w.goingHome) {
        // arrived at the errand's destination: head back to the yard
        [w.tx, w.ty] = homeOf(w.slot);
        w.goingHome = false;
        w.homing = true;
      } else if (w.homing) {
        w.homing = false;
        if (w.state !== "busy") w.state = "idle";
      }
    }
    const target = this.openCircuits.size > 0 ? 1 : 0;
    this.gate += (target - this.gate) * Math.min(1, dt * 8);
    if (Math.abs(target - this.gate) < 0.001) this.gate = target;
    this.plannerPulse = Math.max(0, this.plannerPulse - dt * 2);
    this.queuePulse = Math.max(0, this.queuePulse - dt * 2);
  }

  summary(): SceneSummary {
    let workers = 0;
    let busy = 0;
    for (const w of this.pool) {
      if (!w.active || w.dying > 0) continue;
      workers++;
      if (w.state === "busy") busy++;
    }
    return {
      workers,
      busy,
      queue: this.queueDepth,
      done: this.done,
      dead: this.dead,
      circuit: this.openCircuits.size > 0 ? "tripped" : "ok",
      approvals: this.approvals.length,
    };
  }
}
