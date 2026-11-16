import { describe, expect, it } from "vitest";
import { LAYOUT, ROADS_SAMPLED, doorWorld, shortestPath, type BuildingSpec, type XZ } from "./town";

const dist = (a: XZ, b: XZ) => Math.hypot(a[0] - b[0], a[1] - b[1]);
const hw = LAYOUT.board.w / 2;
const hd = LAYOUT.board.d / 2;

/** Distance from a point to a rotated rectangle (0 inside). */
function toBuilding(p: XZ, b: BuildingSpec): number {
  const a = (b.rot * Math.PI) / 180;
  const dx = p[0] - b.x, dz = p[1] - b.z;
  const lx = dx * Math.cos(a) - dz * Math.sin(a);
  const lz = dx * Math.sin(a) + dz * Math.cos(a);
  return Math.hypot(Math.max(Math.abs(lx) - b.w / 2, 0), Math.max(Math.abs(lz) - b.d / 2, 0));
}
function corners(b: BuildingSpec): XZ[] {
  return ([[-1, -1], [1, -1], [1, 1], [-1, 1]] as XZ[]).map(([sx, sz]) => doorWorld(b, [(sx * b.w) / 2, (sz * b.d) / 2]));
}
// separating axis test for two rotated rectangles, with a clearance margin
function overlap(a: BuildingSpec, b: BuildingSpec, margin: number): boolean {
  const grow = (s: BuildingSpec) => ({ ...s, w: s.w + margin, d: s.d + margin });
  const A = corners(grow(a)), B = corners(grow(b));
  for (const poly of [A, B]) {
    for (let i = 0; i < 4; i++) {
      const p = poly[i], q = poly[(i + 1) % 4];
      const nx = -(q[1] - p[1]), nz = q[0] - p[0];
      const pr = (c: XZ[]) => c.map((v) => v[0] * nx + v[1] * nz);
      const [pa, pb] = [pr(A), pr(B)];
      if (Math.max(...pa) < Math.min(...pb) || Math.max(...pb) < Math.min(...pa)) return false;
    }
  }
  return true;
}
/** Distance from a point to the drawn road surface (centreline distance minus half width). */
function toRoad(p: XZ): number {
  let best = Infinity;
  for (const { road, pts } of ROADS_SAMPLED) {
    if (road.interior) continue;
    for (const q of pts) best = Math.min(best, dist(p, q) - road.width / 2);
  }
  return best;
}

describe("layout file", () => {
  it("has the expected shape", () => {
    expect(LAYOUT.board).toMatchObject({ w: 40, d: 30 });
    expect(LAYOUT.buildings.map((b) => b.id).sort()).toEqual(["done", "dlq", "downstream", "planner", "queue", "workers"]);
    for (const r of LAYOUT.roads) {
      expect(["normal", "retry", "dead"]).toContain(r.type);
      expect(r.width).toBeGreaterThan(0.5);
      expect(r.points.length).toBeGreaterThanOrEqual(2);
      expect(LAYOUT.nodes[r.from]).toBeDefined();
      expect(LAYOUT.nodes[r.to]).toBeDefined();
      expect(dist(r.points[0], LAYOUT.nodes[r.from])).toBeLessThan(0.01);
      expect(dist(r.points.at(-1)!, LAYOUT.nodes[r.to])).toBeLessThan(0.01);
    }
    for (const p of LAYOUT.props) expect(Number.isFinite(p.x + p.z + p.s)).toBe(true);
  });
  it("keeps everything on the board", () => {
    for (const b of LAYOUT.buildings) for (const c of corners(b)) {
      expect(Math.abs(c[0])).toBeLessThan(hw - 0.5);
      expect(Math.abs(c[1])).toBeLessThan(hd - 0.5);
    }
    for (const { pts } of ROADS_SAMPLED) for (const p of pts) {
      expect(Math.abs(p[0])).toBeLessThan(hw - 1);
      expect(Math.abs(p[1])).toBeLessThan(hd - 1);
    }
    for (const i of LAYOUT.islands) {
      expect(Math.abs(i.x) + i.r).toBeLessThan(hw);
      expect(Math.abs(i.z) + i.r).toBeLessThan(hd);
    }
  });
});

describe("buildings and doors", () => {
  it("no two buildings overlap (with 1.5 units of clearance)", () => {
    const bs = LAYOUT.buildings;
    for (let i = 0; i < bs.length; i++) for (let j = i + 1; j < bs.length; j++) {
      expect(overlap(bs[i], bs[j], 1.5), `${bs[i].id} vs ${bs[j].id}`).toBe(false);
    }
  });
  it("every door node sits where its building says, just outside it, and is on the graph", () => {
    const onGraph = new Set(LAYOUT.roads.flatMap((r) => [r.from, r.to]));
    for (const b of LAYOUT.buildings) {
      expect(Object.keys(b.doors).length).toBeGreaterThan(0);
      for (const [node, local] of Object.entries(b.doors)) {
        expect(dist(doorWorld(b, local), LAYOUT.nodes[node]), node).toBeLessThan(0.02);
        expect(toBuilding(LAYOUT.nodes[node], b), node).toBeGreaterThan(0.3);
        expect(toBuilding(LAYOUT.nodes[node], b), node).toBeLessThan(1.2);
        expect(onGraph.has(node), node).toBe(true);
      }
    }
  });
  it("roads do not run through buildings (interior paths excepted)", () => {
    for (const { road, pts } of ROADS_SAMPLED) {
      if (road.interior) continue;
      for (const b of LAYOUT.buildings) for (const p of pts) {
        expect(toBuilding(p, b) - road.width / 2, `${road.id} / ${b.id}`).toBeGreaterThan(-0.35);
      }
    }
  });
});

describe("path graph: every event route exists", () => {
  const go = (a: string, b: string) => shortestPath(a, b);
  it("the whole graph is connected from the Planner", () => {
    for (const n of Object.keys(LAYOUT.nodes)) expect(go("planner_door", n), n).not.toBeNull();
  });
  it("queue to worker, worker to downstream, downstream to done", () => {
    expect(go("queue_out", "workers_in")!.roads).toEqual(["r2_queue_workers"]);
    expect(go("workers_in", "downstream_in")!.roads).toEqual(["i_workers", "r3_workers_gate", "r4_gate_downstream"]);
    expect(go("downstream_in", "done_in")!.roads).toEqual(["i_downstream", "r5_downstream_done"]);
  });
  it("retry loop: Downstream back to the Queue over the retry road", () => {
    const p = go("downstream_in", "queue_retry")!;
    expect(p.roads).toContain("r6_retry");
    expect(p.roads.filter((r) => LAYOUT.roads.find((x) => x.id === r)!.type === "dead")).toEqual([]);
  });
  it("dead job: Downstream to the DLQ over the dead road", () => {
    expect(go("downstream_in", "dlq_in")!.roads).toContain("r7_dead");
  });
  it("robots can get home from Done, the DLQ and the Queue", () => {
    for (const from of ["done_in", "dlq_in", "queue_retry"]) expect(go(from, "workers_in"), from).not.toBeNull();
  });
});

describe("one readable route", () => {
  it("normal, retry and dead roads only meet at shared nodes (no crossings)", () => {
    const real = ROADS_SAMPLED.filter((r) => !r.road.interior);
    const seg = (a: XZ, b: XZ, c: XZ, d: XZ) => {
      const o = (p: XZ, q: XZ, r: XZ) => (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]);
      return o(a, b, c) * o(a, b, d) < 0 && o(c, d, a) * o(c, d, b) < 0;
    };
    for (let i = 0; i < real.length; i++) for (let j = i + 1; j < real.length; j++) {
      const A = real[i], B = real[j];
      for (let a = 1; a < A.pts.length; a++) for (let b = 1; b < B.pts.length; b++) {
        expect(seg(A.pts[a - 1], A.pts[a], B.pts[b - 1], B.pts[b]), `${A.road.id} x ${B.road.id}`).toBe(false);
      }
    }
  });
  it("the main route runs Planner -> Queue -> Workers -> Downstream -> Done in one direction", () => {
    const order = ["planner_door", "queue_in", "queue_out", "workers_in", "workers_out", "j_fail", "downstream_in", "downstream_out", "done_in"];
    let at = 0;
    for (const a of order.slice(0, -1)) {
      const next = order[order.indexOf(a) + 1];
      const p = shortestPath(a, next)!;
      at += p.length;
      expect(p.length).toBeGreaterThan(0);
    }
    expect(at).toBeGreaterThan(30);
    // each normal, non-interior main road is declared from the upstream node to the downstream one
    const main = LAYOUT.roads.filter((r) => r.type === "normal").map((r) => [r.from, r.to]);
    for (const [from, to] of main) expect(order.indexOf(from)).toBeLessThan(order.indexOf(to));
  });
});

describe("decoration", () => {
  it("no prop touches a road, a building, a gate or the worker paths", () => {
    const interior = ROADS_SAMPLED.filter((r) => r.road.interior);
    for (const p of LAYOUT.props) {
      const at: XZ = [p.x, p.z];
      const reach = p.kind === "flower" ? 0.2 : p.kind === "tree" ? 0.7 * p.s : 0.35 * p.s;
      expect(toRoad(at), `${p.kind} ${p.x},${p.z} on a road`).toBeGreaterThan(reach);
      for (const b of LAYOUT.buildings) expect(toBuilding(at, b), `${p.kind} in ${b.id}`).toBeGreaterThan(reach);
      for (const g of [LAYOUT.gates.circuit, LAYOUT.gates.approval]) expect(dist(at, [g.x, g.z])).toBeGreaterThan(1.8);
      for (const { pts } of interior) for (const q of pts) expect(dist(at, q)).toBeGreaterThan(reach + 0.3);
    }
  });
  it("stays under the decoration budget (trees, bushes, rocks, flowers, benches, lamps)", () => {
    const n = (k: string) => LAYOUT.props.filter((p) => p.kind === k).length;
    // triangles: tree 80+24, bush 20, rock 20, flower 12, bench 48, lamp 40
    const tris = n("tree") * 104 + n("bush") * 20 + n("rock") * 20 + n("flower") * 12 + n("bench") * 48 + n("lamp") * 40;
    expect(tris).toBeLessThan(8000);
  });
  it("gates stand on their roads", () => {
    for (const g of [LAYOUT.gates.circuit, LAYOUT.gates.approval]) {
      const road = ROADS_SAMPLED.find((r) => r.road.id === g.on)!;
      expect(Math.min(...road.pts.map((q) => dist(q, [g.x, g.z])))).toBeLessThan(0.6);
    }
  });
});
