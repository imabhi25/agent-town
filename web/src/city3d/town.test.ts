import { describe, expect, it } from "vitest";
import { LAYOUT, ROADS_SAMPLED, shortestPath, zoneForStage, zoneLabelAt, type XZ } from "./town";

const dist = (a: XZ, b: XZ) => Math.hypot(a[0] - b[0], a[1] - b[1]);
const hw = LAYOUT.board.w / 2;
const hd = LAYOUT.board.d / 2;
const paved = ROADS_SAMPLED.filter((r) => r.road.type === "street" || r.road.type === "lane");

function toSegment(p: XZ, a: XZ, b: XZ): number {
  const vx = b[0] - a[0], vz = b[1] - a[1];
  const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vz) / (vx * vx + vz * vz || 1e-9)));
  return dist(p, [a[0] + vx * t, a[1] + vz * t]);
}
/** Distance from a point to the edge of the streets and lanes (negative = on one). */
function toStreet(p: XZ): number {
  let best = Infinity;
  for (const r of LAYOUT.roads) {
    if (r.type !== "street" && r.type !== "lane") continue;
    for (let i = 1; i < r.points.length; i++) best = Math.min(best, toSegment(p, r.points[i - 1], r.points[i]) - r.width / 2);
  }
  return best;
}
/** Distance outside a block's rectangle (negative = inside). */
const outsideBlock = (p: XZ, z: { x: number; z: number; w: number; d: number }) =>
  Math.max(Math.abs(p[0] - z.x) - z.w / 2, Math.abs(p[1] - z.z) - z.d / 2);
/** Distance outside the square, the paved area inside the loop of streets. */
const outsideSquare = (p: XZ) => Math.max(Math.abs(p[0]) - LAYOUT.plaza.w / 2, Math.abs(p[1]) - LAYOUT.plaza.d / 2);

describe("layout file", () => {
  it("has 6 blocks of 8 lots, a square with a fountain and a town hall, and a seed", () => {
    expect(LAYOUT.board).toMatchObject({ w: 34, d: 32 });
    expect(LAYOUT.zones.map((z) => z.stage)).toEqual([1, 2, 3, 4, 5, 6]);
    for (const z of LAYOUT.zones) expect(z.slots).toHaveLength(8);
    expect(LAYOUT.plaza.pads).toHaveLength(8);
    expect(outsideSquare(LAYOUT.plaza.fountain)).toBeLessThan(-1);
    expect(outsideSquare([LAYOUT.plaza.hall.x, LAYOUT.plaza.hall.z])).toBeLessThan(-1);
    expect(LAYOUT.seed).toBe(11);
  });
  it("every road joins two known nodes at its own end points", () => {
    for (const r of LAYOUT.roads) {
      expect(["street", "lane", "plaza", "foot"]).toContain(r.type);
      expect(dist(r.points[0], LAYOUT.nodes[r.from]), r.id).toBeLessThan(0.01);
      expect(dist(r.points.at(-1)!, LAYOUT.nodes[r.to]), r.id).toBeLessThan(0.01);
    }
  });
  it("keeps the blocks and streets on the board", () => {
    for (const z of LAYOUT.zones) {
      expect(Math.abs(z.x) + z.w / 2, z.id).toBeLessThan(hw);
      expect(Math.abs(z.z) + z.d / 2, z.id).toBeLessThan(hd);
    }
    for (const { pts } of paved) for (const p of pts) {
      expect(Math.abs(p[0])).toBeLessThan(hw);
      expect(Math.abs(p[1])).toBeLessThan(hd);
    }
  });
});

describe("blocks and lots", () => {
  it("blocks overlap neither each other, the square nor the streets", () => {
    const zs = LAYOUT.zones;
    for (let i = 0; i < zs.length; i++) {
      const a = zs[i];
      expect(Math.max(Math.abs(a.x) - a.w / 2 - LAYOUT.plaza.w / 2, Math.abs(a.z) - a.d / 2 - LAYOUT.plaza.d / 2), a.id).toBeGreaterThan(0);
      for (let j = i + 1; j < zs.length; j++) {
        const b = zs[j];
        const gap = Math.max(Math.abs(a.x - b.x) - (a.w + b.w) / 2, Math.abs(a.z - b.z) - (a.d + b.d) / 2);
        expect(gap, `${a.id} ${b.id}`).toBeGreaterThan(0.5);
      }
      for (const { road, pts } of paved) {
        if (road.type === "lane" && road.id === `lane${a.stage}`) continue;
        for (const q of pts) expect(outsideBlock(q, a) + road.width / 2, `${road.id} / ${a.id}`).toBeGreaterThan(0);
      }
    }
  });
  it("every lot lies inside its block, and no two lots overlap", () => {
    const all = LAYOUT.zones.flatMap((z) => z.slots.map((s) => ({ z, s })));
    for (const { z, s } of all) expect(outsideBlock([s.x, s.z], z) + s.size / 2, s.id).toBeLessThan(0.1);
    for (let i = 0; i < all.length; i++) for (let j = i + 1; j < all.length; j++) {
      const a = all[i].s, b = all[j].s;
      expect(Math.max(Math.abs(a.x - b.x), Math.abs(a.z - b.z)), `${a.id} ${b.id}`).toBeGreaterThan(a.size + 0.3);
    }
  });
  it("the first tasks get the front, central lots (nearest the street)", () => {
    for (const z of LAYOUT.zones) {
      const street = Math.sign(z.z) * 5;
      const d = z.slots.map((s) => Math.abs(s.z - street));
      expect(Math.max(...d.slice(0, 4)), z.id).toBeLessThan(Math.min(...d.slice(4)));
      expect(Math.abs(z.slots[0].x - z.x), z.id).toBeLessThan(Math.abs(z.slots[2].x - z.x));
    }
  });
  it("a block's label sits just past its outer edge, away from the square", () => {
    for (const z of LAYOUT.zones) {
      const [x, , lz] = zoneLabelAt(z);
      expect(x).toBe(z.x);
      expect(Math.abs(lz), z.id).toBeGreaterThan(Math.abs(z.z) + z.d / 2);
      expect(Math.abs(lz), z.id).toBeLessThan(hd);
    }
  });
});

describe("path graph", () => {
  it("every lot is reachable from the Foreman, the idle pads and every other block", () => {
    for (const z of LAYOUT.zones) for (const s of z.slots) {
      expect(shortestPath("foreman", s.id), s.id).not.toBeNull();
      expect(shortestPath("pad1", s.id), s.id).not.toBeNull();
      expect(shortestPath(s.id, "pad8"), s.id).not.toBeNull();
    }
    expect(shortestPath("z1_s1", "z6_s8")).not.toBeNull();
  });
  it("every node is on the graph", () => {
    const on = new Set(LAYOUT.roads.flatMap((r) => [r.from, r.to]));
    for (const n of Object.keys(LAYOUT.nodes)) expect(on.has(n), n).toBe(true);
  });
  it("a worker enters a block through its own lane and gate", () => {
    for (const z of LAYOUT.zones) {
      const p = shortestPath("pad1", z.slots[0].id)!;
      expect(p.roads, z.id).toContain(`lane${z.stage}`);
      expect(p.roads, z.id).toContain(`in${z.stage}`);
    }
  });
  it("barriers stand on their block's lane", () => {
    expect(LAYOUT.gates).toHaveLength(6);
    for (const g of LAYOUT.gates) {
      const road = ROADS_SAMPLED.find((r) => r.road.id === g.road)!;
      expect(Math.min(...road.pts.map((q) => dist(q, [g.x, g.z])))).toBeLessThan(0.5);
    }
  });
});

describe("stages map to blocks", () => {
  it("stage 1..6 get their own block and later stages merge into the last", () => {
    expect(zoneForStage(1).id).toBe("z1");
    expect(zoneForStage(6).id).toBe("z6");
    expect(zoneForStage(7).id).toBe("z6");
    expect(zoneForStage(0).id).toBe("z1");
  });
  it("the preview plan fits the blocks and lots", () => {
    for (const p of LAYOUT.preview.zones) {
      expect(p.stage).toBeLessThanOrEqual(LAYOUT.zones.length);
      expect(p.tasks).toBeLessThanOrEqual(LAYOUT.zones[p.stage - 1].slots.length);
    }
  });
});

describe("decoration", () => {
  it("trees, bushes, rocks and flowers stay off the streets, the blocks and the square", () => {
    for (const p of LAYOUT.props.filter((q) => q.kind !== "lamp")) {
      const at: XZ = [p.x, p.z];
      const tag = `${p.kind} ${p.x},${p.z}`;
      expect(toStreet(at), tag).toBeGreaterThan(0.5);
      for (const z of LAYOUT.zones) expect(outsideBlock(at, z), tag).toBeGreaterThan(0.25);
      expect(outsideSquare(at), tag).toBeGreaterThan(0.25);
      for (const g of LAYOUT.gates) expect(dist(at, [g.x, g.z]), tag).toBeGreaterThan(1);
    }
  });
  it("lamps stand at the side of a street, not on it and not in a block entrance", () => {
    const lamps = LAYOUT.props.filter((p) => p.kind === "lamp");
    expect(lamps.length).toBeGreaterThan(10);
    for (const p of lamps) {
      const d = toStreet([p.x, p.z]);
      expect(d, `${p.x},${p.z}`).toBeGreaterThan(0.1);
      expect(d, `${p.x},${p.z}`).toBeLessThan(0.6);
      for (const g of LAYOUT.gates) expect(dist([p.x, p.z], [g.x, g.z])).toBeGreaterThan(1);
    }
  });
  it("has the pines round the edge of the board", () => {
    const edge = LAYOUT.props.filter((p) => p.kind === "tree" && (Math.abs(p.x) > hw - 2 || Math.abs(p.z) > hd - 3));
    expect(edge.length).toBeGreaterThan(30);
  });
});
