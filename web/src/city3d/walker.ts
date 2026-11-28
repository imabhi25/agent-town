// Where a worker wants to be and how it gets there. Pure (no three.js, no React): a worker is a
// point that walks along road polylines from `shortestPath`, and it only moves when a model change
// gave it a new target node. Nothing wanders on a timer.
import { LAYOUT, shortestPath, type XZ } from "./town";
import type { TownModel, WorkerView } from "./townModel";

export const WALK_SPEED = 7; // metres per second at rest: a robot reaches any zone in about 2s, well inside a 4s build. Change here.
export const CATCH_UP = 12; // metres: farther than this behind and the worker hurries, so replay never lags

export class Walker {
  x: number;
  z: number;
  heading = 0;
  moving = false;
  target = ""; // node id this walker is heading to or standing at
  private pts: XZ[] = [];
  private cum: number[] = [0];
  private d = 0;
  private last = ""; // last node reached (walks restart from here when the target changes mid-road)

  constructor(node: string) {
    const [x, z] = LAYOUT.nodes[node];
    this.x = x;
    this.z = z;
    this.target = node;
    this.last = node;
  }

  get remaining(): number {
    return this.cum[this.cum.length - 1] - this.d;
  }

  /** Head for `node`. Same target = no change; a new one restarts from the current position. */
  go(node: string): void {
    if (node === this.target || !LAYOUT.nodes[node]) return;
    this.target = node;
    const path = shortestPath(this.last, node);
    if (!path) return;
    this.pts = [[this.x, this.z], ...path.points];
    this.cum = [0];
    for (let i = 1; i < this.pts.length; i++) {
      this.cum.push(this.cum[i - 1] + Math.hypot(this.pts[i][0] - this.pts[i - 1][0], this.pts[i][1] - this.pts[i - 1][1]));
    }
    this.d = 0;
    this.moving = this.remaining > 1e-6;
  }

  step(dt: number): void {
    if (!this.moving) return;
    const speed = WALK_SPEED * (1 + Math.max(0, this.remaining - CATCH_UP) / CATCH_UP);
    this.d = Math.min(this.cum[this.cum.length - 1], this.d + speed * dt);
    let i = 1;
    while (i < this.cum.length - 1 && this.cum[i] < this.d) i++;
    const seg = this.cum[i] - this.cum[i - 1];
    const t = seg > 1e-9 ? (this.d - this.cum[i - 1]) / seg : 1;
    const [ax, az] = this.pts[i - 1];
    const [bx, bz] = this.pts[i];
    if (seg > 1e-9) this.heading = Math.atan2(bx - ax, bz - az);
    this.x = ax + (bx - ax) * t;
    this.z = az + (bz - az) * t;
    if (this.d >= this.cum[this.cum.length - 1]) {
      this.moving = false;
      this.last = this.target;
    }
  }
}

/** The node a worker should stand at: its task's slot, the Foreman for a task-less job, else a pad. */
export function targetNode(model: TownModel, w: WorkerView, order: number): string {
  if (w.taskId) {
    const t = model.tasks.find((x) => x.id === w.taskId);
    if (t) return `z${t.zone}_s${t.slot + 1}`;
  }
  if (w.jobId) return "foreman";
  return `pad${(order % LAYOUT.plaza.pads.length) + 1}`;
}
