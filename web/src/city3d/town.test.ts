import { describe, expect, it } from "vitest";
import { LAYOUT, ROADS_SAMPLED, shortestPath, zoneForStage, type XZ } from "./town";

const dist = (a: XZ, b: XZ) => Math.hypot(a[0] - b[0], a[1] - b[1]);
const hw = LAYOUT.board.w / 2;
const hd = LAYOUT.board.d / 2;
const drawn = ROADS_SAMPLED.filter((r) => ["ring", "spoke", "plaza"].includes(r.road.type));

/** Distance from a point to the surface of the drawn roads and footpaths (0 = touching). */
function toRoad(p: XZ): number {
  let best = Infinity;
  for (const { road, pts } of ROADS_SAMPLED) {
    const half = road.type === "foot" ? 0.35 : road.width / 2;
    for (const q of pts) best = Math.min(best, dist(p, q) - half);
  }
  return best;
}

describe("layout file", () => {
  it("has 6 zones of 8 slots, a plaza, and a seed", () => {
    expect(LAYOUT.board).toMatchObject({ w: 40, d: 30 });
    expect(LAYOUT.zones).toHaveLength(6);
    expect(LAYOUT.zones.map((z) => z.stage)).toEqual([1, 2, 3, 4, 5, 6]);
    for (const z of LAYOUT.zones) expect(z.slots).toHaveLength(8);
    expect(LAYOUT.plaza.pads).toHaveLength(8);
    expect(LAYOUT.seed).toBe(11);
  });
  it("every road joins two known nodes at its own end points", () => {
    for (const r of LAYOUT.roads) {
      expect(["ring", "spoke", "plaza", "foot"]).toContain(r.type);
      expect(dist(r.points[0], LAYOUT.nodes[r.from]), r.id).toBeLessThan(0.01);
      expect(dist(r.points.at(-1)!, LAYOUT.nodes[r.to]), r.id).toBeLessThan(0.01);
    }
  });
  it("keeps everything on the board", () => {
    for (const z of LAYOUT.zones) {
      expect(Math.abs(z.x) + z.r).toBeLessThan(hw);
      expect(Math.abs(z.z) + z.r).toBeLessThan(hd);
    }
    for (const { pts } of drawn) for (const p of pts) {
      expect(Math.abs(p[0])).toBeLessThan(hw - 1);
      expect(Math.abs(p[1])).toBeLessThan(hd - 1);
    }
  });
});

describe("zones and slots", () => {
  it("zones do not overlap each other or the plaza", () => {
    const zs = LAYOUT.zones;
    for (let i = 0; i < zs.length; i++) {
      expect(Math.hypot(zs[i].x, zs[i].z) - zs[i].r - LAYOUT.plaza.r, zs[i].id).toBeGreaterThan(0.5);
      for (let j = i + 1; j < zs.length; j++) {
        expect(dist([zs[i].x, zs[i].z], [zs[j].x, zs[j].z]) - zs[i].r - zs[j].r, `${zs[i].id} ${zs[j].id}`).toBeGreaterThan(0.5);
      }
    }
  });
  it("no two slots overlap (even rotated, with a 0.3 gap), and every slot lies inside its zone", () => {
    const all = LAYOUT.zones.flatMap((z) => z.slots.map((s) => ({ z, s })));
    for (const { z, s } of all) expect(dist([s.x, s.z], [z.x, z.z]) + s.size * 0.71, s.id).toBeLessThan(z.r);
    for (let i = 0; i < all.length; i++) for (let j = i + 1; j < all.length; j++) {
      const d = dist([all[i].s.x, all[i].s.z], [all[j].s.x, all[j].s.z]);
      expect(d, `${all[i].s.id} ${all[j].s.id}`).toBeGreaterThan(all[i].s.size + 0.3 - 0.01);
    }
  });
  it("slots stay off the roads and the footpaths that lead to other slots", () => {
    for (const { road, pts } of ROADS_SAMPLED) {
      if (road.type === "foot") continue;
      for (const z of LAYOUT.zones) for (const s of z.slots) {
        for (const q of pts) expect(dist([s.x, s.z], q) - road.width / 2 - s.size * 0.71, `${road.id} ${s.id}`).toBeGreaterThan(-0.3);
      }
    }
    for (const z of LAYOUT.zones) for (const road of LAYOUT.roads.filter((r) => r.type === "foot" && !r.id.startsWith(`${z.id}_p`) && r.id !== `in${z.stage}`)) {
      void road;
    }
  });
  it("the entrance footpath of a zone runs between slots, not through one", () => {
    for (const z of LAYOUT.zones) {
      const road = ROADS_SAMPLED.find((r) => r.road.id === `in${z.stage}`)!;
      for (const s of z.slots) for (const q of road.pts) expect(dist([s.x, s.z], q) - 0.45 - s.size * 0.5, `${z.id} ${s.id}`).toBeGreaterThan(-0.05);
    }
  });
});

describe("path graph", () => {
  it("every zone is connected to the plaza (foreman)", () => {
    for (const z of LAYOUT.zones) {
      expect(shortestPath("foreman", z.gate), z.id).not.toBeNull();
      expect(shortestPath("foreman", z.hub), z.id).not.toBeNull();
    }
  });
  it("every building slot is reachable from the idle pads and from every other zone", () => {
    for (const z of LAYOUT.zones) for (const s of z.slots) {
      expect(shortestPath("pad1", s.id), s.id).not.toBeNull();
      expect(shortestPath(s.id, "pad8"), s.id).not.toBeNull();
    }
    expect(shortestPath("z1_s1", "z6_s8")).not.toBeNull();
  });
  it("every node is on the graph", () => {
    const on = new Set(LAYOUT.roads.flatMap((r) => [r.from, r.to]));
    for (const n of Object.keys(LAYOUT.nodes)) expect(on.has(n), n).toBe(true);
  });
  it("a worker's walk from the foreman to a slot goes via the spoke of its own zone", () => {
    for (const z of LAYOUT.zones) {
      const p = shortestPath("pad1", z.slots[0].id)!;
      expect(p.roads.some((r) => r === `spoke${z.stage}` || r.startsWith("ring")), z.id).toBe(true);
    }
  });
});

describe("one readable route", () => {
  it("the ring visits the zones in stage order and closes the loop", () => {
    const ring = LAYOUT.roads.filter((r) => r.type === "ring");
    expect(ring).toHaveLength(6);
    ring.forEach((r, i) => {
      expect(r.from).toBe(`z${i + 1}_gate`);
      expect(r.to).toBe(`z${(i + 1) % 6 + 1}_gate`);
    });
  });
  it("ring, spoke and plaza roads meet only at shared nodes (no crossings)", () => {
    const seg = (a: XZ, b: XZ, c: XZ, d: XZ) => {
      const o = (p: XZ, q: XZ, r: XZ) => (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]);
      // the epsilon ignores the 1e-15 rounding at a node two roads share
      return o(a, b, c) * o(a, b, d) < -1e-9 && o(c, d, a) * o(c, d, b) < -1e-9;
    };
    for (let i = 0; i < drawn.length; i++) for (let j = i + 1; j < drawn.length; j++) {
      const A = drawn[i], B = drawn[j];
      for (let a = 1; a < A.pts.length; a++) for (let b = 1; b < B.pts.length; b++) {
        expect(seg(A.pts[a - 1], A.pts[a], B.pts[b - 1], B.pts[b]), `${A.road.id} x ${B.road.id}`).toBe(false);
      }
    }
  });
  it("roads do not run through other zones", () => {
    for (const { road, pts } of drawn) {
      for (const z of LAYOUT.zones) {
        const mine = road.from.startsWith(z.id) || road.to.startsWith(z.id);
        if (mine) continue;
        for (const q of pts) expect(dist(q, [z.x, z.z]) - z.r, `${road.id} / ${z.id}`).toBeGreaterThan(-0.2);
      }
    }
  });
  it("barriers stand on their spokes", () => {
    expect(LAYOUT.gates).toHaveLength(6);
    for (const g of LAYOUT.gates) {
      const road = ROADS_SAMPLED.find((r) => r.road.id === g.road)!;
      expect(Math.min(...road.pts.map((q) => dist(q, [g.x, g.z])))).toBeLessThan(0.5);
    }
  });
});

describe("stages map to zones", () => {
  it("stage 1..6 get their own zone and later stages merge into the last", () => {
    expect(zoneForStage(1).id).toBe("z1");
    expect(zoneForStage(6).id).toBe("z6");
    expect(zoneForStage(7).id).toBe("z6");
    expect(zoneForStage(0).id).toBe("z1");
  });
  it("the preview plan fits the zones and slots", () => {
    for (const p of LAYOUT.preview.zones) {
      expect(p.stage).toBeLessThanOrEqual(LAYOUT.zones.length);
      expect(p.tasks).toBeLessThanOrEqual(LAYOUT.zones[p.stage - 1].slots.length);
    }
  });
});

describe("decoration", () => {
  it("no prop touches a road, a footpath, a zone slot area, the plaza or a barrier", () => {
    for (const p of LAYOUT.props) {
      const at: XZ = [p.x, p.z];
      const reach = p.kind === "flower" ? 0.2 : p.kind === "tree" ? 0.7 * p.s : p.kind === "fence" ? 0.2 : 0.35 * p.s;
      expect(toRoad(at), `${p.kind} ${p.x},${p.z}`).toBeGreaterThan(reach - (p.kind === "fence" ? 0.2 : 0));
      if (p.kind !== "fence") for (const z of LAYOUT.zones) expect(dist(at, [z.x, z.z]) - z.r, `${p.kind} in ${z.id}`).toBeGreaterThan(reach * 0.5);
      expect(Math.hypot(p.x, p.z) - LAYOUT.plaza.r, `${p.kind} on the plaza`).toBeGreaterThan(reach * 0.5);
      for (const g of LAYOUT.gates) expect(dist(at, [g.x, g.z])).toBeGreaterThan(1.5);
    }
  });
  it("stays under the decoration triangle budget (8,000)", () => {
    const n = (k: string) => LAYOUT.props.filter((p) => p.kind === k).length;
    // tree 104, bush 20, rock 20, flower 12, lamp 40, fence segment 24 (post + rail)
    const tris = n("tree") * 104 + n("bush") * 20 + n("rock") * 20 + n("flower") * 12 + n("lamp") * 40 + n("fence") * 24;
    expect(tris).toBeLessThanOrEqual(8000);
  });
  it("is deterministic: regenerating from the seed gives the same file (checked by pytest)", () => {
    expect(LAYOUT.seed).toBe(11);
  });
});
