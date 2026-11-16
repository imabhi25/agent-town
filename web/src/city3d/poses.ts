// Turns the headless Scene (which already maps events to worker state) into 3D poses: where each
// robot stands, which way it faces, what it carries. No three.js, no allocation per frame, and
// nothing moves unless an event put it in motion: the Scene decides, this only draws it on roads.
import { homeOf, MAX_WORKERS, placeOf, type Place, type Scene } from "../scene";
import type { WorkerStateName } from "../palette";
import { toX, toZ } from "./layout";
import { ROUTE_TABLE, type RoutePlace } from "./routes";

const MAXPTS = 12;
const FLY_SECONDS = 0.5;
const SPAWN_SECONDS = 0.35;
export const MAX_FLY = 12;
export const PLACES_3D: Place[] = ["queue", "downstream", "done", "dlq"];

export class Pose {
  visible = false;
  x = 0;
  z = 0;
  bob = 0;
  heading = 0;
  scale = 1;
  state: WorkerStateName = "idle";
  carrying = false;
  // route following (scratch arrays, reused)
  known = false;
  place: RoutePlace = "yard";
  tx = NaN;
  ty = NaN;
  sx = 0;
  sy = 0;
  total = 0;
  n = 0;
  pts = new Float32Array(MAXPTS * 2);
  cum = new Float32Array(MAXPTS);
  jobId: string | null = null;
  spawn = 1;
  phase = 0;
}

export class Fly {
  active = false;
  worker = -1;
  t = 0;
  x = 0;
  y = 0;
  z = 0;
}

/** Which place a Scene destination belongs to (queue/downstream/done/dlq, else the yard). */
export function placeOfTarget(tx: number, ty: number, slot: number): RoutePlace {
  for (const p of PLACES_3D) {
    const [px, py] = placeOf(p, slot);
    if (px === tx && py === ty) return p;
  }
  return "yard";
}

export class Poses {
  readonly list: Pose[] = Array.from({ length: MAX_WORKERS }, () => new Pose());
  readonly fly: Fly[] = Array.from({ length: MAX_FLY }, () => new Fly());
  readonly door = { x: toX(placeOf("queue", 2)[0]), z: toZ(placeOf("queue", 2)[1]) }; // jobs leave the Queue here

  update(scene: Scene, dt: number): void {
    for (let i = 0; i < MAX_WORKERS; i++) {
      const w = scene.pool[i];
      const p = this.list[i];
      if (!w.active) {
        p.visible = false;
        p.known = false;
        p.jobId = null;
        continue;
      }
      if (!p.known) this.spawn(p, w.slot, w.x, w.y, w.tx, w.ty);
      p.visible = true;
      if (w.tx !== p.tx || w.ty !== p.ty) this.retarget(p, w.slot, w.x, w.y, w.tx, w.ty);
      this.follow(p, w.x, w.y, dt);
      p.state = w.state;
      p.carrying = w.dying === 0 && (w.state === "busy" || (w.goingHome && !w.homing));
      p.spawn = Math.min(1, p.spawn + dt / SPAWN_SECONDS);
      p.scale = (w.dying > 0 ? 0.6 + 0.4 * w.dying : 1) * (0.4 + 0.6 * p.spawn);
      if (w.jobId !== p.jobId) {
        p.jobId = w.jobId;
        if (w.jobId) this.launch(i, p);
      }
    }
    for (let i = 0; i < MAX_FLY; i++) {
      const f = this.fly[i];
      if (!f.active) continue;
      f.t += dt / FLY_SECONDS;
      const target = this.list[f.worker];
      if (f.t >= 1 || !target.visible) {
        f.active = false;
        continue;
      }
      const e = f.t * f.t * (3 - 2 * f.t); // smoothstep
      f.x = this.door.x + (target.x - this.door.x) * e;
      f.z = this.door.z + (target.z - this.door.z) * e;
      f.y = 0.4 + Math.sin(Math.PI * f.t) * 1.3 + (1.6 * e);
    }
  }

  private spawn(p: Pose, slot: number, wx: number, wy: number, tx: number, ty: number): void {
    const [hx, hy] = homeOf(slot);
    p.known = true;
    p.x = toX(hx);
    p.z = toZ(hy);
    p.place = placeOfTarget(tx, ty, slot);
    p.tx = tx;
    p.ty = ty;
    p.n = 0;
    p.total = 0;
    p.sx = wx;
    p.sy = wy;
    p.spawn = 0;
    p.heading = 0;
  }

  /** The Scene gave this worker a new destination: build the road route to it. */
  private retarget(p: Pose, slot: number, wx: number, wy: number, tx: number, ty: number): void {
    const to = placeOfTarget(tx, ty, slot);
    let k = 0;
    const put = (x: number, z: number) => {
      if (k < MAXPTS) {
        p.pts[2 * k] = x;
        p.pts[2 * k + 1] = z;
        k++;
      }
    };
    put(p.x, p.z);
    for (const [x, z] of ROUTE_TABLE[p.place][to]) put(x, z);
    if (to === "yard") {
      const [hx, hy] = homeOf(slot);
      put(toX(hx), toZ(hy));
    }
    p.cum[0] = 0;
    for (let i = 1; i < k; i++) {
      p.cum[i] = p.cum[i - 1] + Math.hypot(p.pts[2 * i] - p.pts[2 * i - 2], p.pts[2 * i + 1] - p.pts[2 * i - 1]);
    }
    p.n = k;
    p.place = to;
    p.tx = tx;
    p.ty = ty;
    p.sx = wx;
    p.sy = wy;
    p.total = Math.hypot(tx - wx, ty - wy);
  }

  /** Place the robot on its route at the same fraction the Scene has walked of its straight line. */
  private follow(p: Pose, wx: number, wy: number, dt: number): void {
    if (p.n < 2) return;
    const frac = p.total > 1e-6 ? Math.min(1, Math.hypot(wx - p.sx, wy - p.sy) / p.total) : 1;
    const d = frac * p.cum[p.n - 1];
    let i = 1;
    while (i < p.n - 1 && p.cum[i] < d) i++;
    const seg = p.cum[i] - p.cum[i - 1];
    const t = seg > 1e-6 ? (d - p.cum[i - 1]) / seg : 1;
    const ax = p.pts[2 * i - 2], az = p.pts[2 * i - 1], bx = p.pts[2 * i], bz = p.pts[2 * i + 1];
    const nx = ax + (bx - ax) * t;
    const nz = az + (bz - az) * t;
    const moved = Math.hypot(nx - p.x, nz - p.z);
    if (moved > 1e-4) {
      let diff = Math.atan2(nx - p.x, nz - p.z) - p.heading;
      diff = Math.atan2(Math.sin(diff), Math.cos(diff));
      p.heading += diff * Math.min(1, dt * 12);
      p.phase += moved * 9;
    }
    p.bob = moved > 1e-4 ? Math.abs(Math.sin(p.phase)) * 0.07 : 0;
    p.x = nx;
    p.z = nz;
  }

  private launch(workerIndex: number, p: Pose): void {
    const f = this.fly.find((x) => !x.active);
    if (!f) return; // a crowd of jobs at once: the rest simply do not get a flying token
    f.active = true;
    f.worker = workerIndex;
    f.t = 0;
    f.x = this.door.x;
    f.z = this.door.z;
    f.y = 0.4;
    void p;
  }
}
