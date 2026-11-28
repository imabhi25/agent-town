// The town as data. docs/reference/town-layout.json (written by scripts/gen_town_layout.py from a
// fixed seed) is the single source: blocks (zones) and their building lots, the square, streets
// (centrelines with a type), the path graph (a road is an edge between two nodes), barriers and
// decoration. The plan follows the owner's mockup: a square town round a fountain and a town hall.
// Everything here is pure maths (no WebGL), so the layout is unit tested: see town.test.ts.
import { CatmullRomCurve3, Vector3 } from "three";
import raw from "../../../docs/reference/town-layout.json";

export type RoadType = "street" | "lane" | "plaza" | "foot";
export type PropKind = "tree" | "bush" | "rock" | "flower" | "lamp" | "fence";
export type XZ = [number, number];

export interface SlotSpec {
  id: string;
  x: number;
  z: number;
  size: number;
  rot: number;
}
export interface ZoneSpec {
  id: string;
  stage: number; // 1..6: the DAG stage this zone shows
  x: number;
  z: number;
  r: number; // radius that contains the whole block
  w: number; // block size (x)
  d: number; // block size (z)
  gate: string; // node at the block entrance, where its lane meets the street
  hub: string; // node on the lane between the two rows of lots
  slots: SlotSpec[];
}
export interface RoadSpec {
  id: string;
  type: RoadType;
  width: number;
  from: string;
  to: string;
  interior: boolean;
  points: XZ[];
}
export interface BarrierSpec {
  id: string;
  road: string;
  x: number;
  z: number;
  rot: number; // degrees: direction of the road at the barrier
}
export interface PropSpec {
  kind: PropKind;
  x: number;
  z: number;
  s: number;
  c?: number;
  rot?: number;
}
export interface TownLayout {
  version: number;
  seed: number;
  board: { w: number; d: number; corner: number };
  plaza: {
    x: number;
    z: number;
    r: number;
    w: number; // the paved square inside the loop of streets
    d: number;
    fountain: XZ;
    hall: { x: number; z: number; w: number; d: number }; // the town hall behind the fountain
    foreman: XZ;
    podium_r: number;
    pads: XZ[];
  };
  zones: ZoneSpec[];
  nodes: Record<string, XZ>;
  roads: RoadSpec[];
  gates: BarrierSpec[];
  props: PropSpec[];
  preview: { zones: { stage: number; name: string; tasks: number }[] };
}

export const LAYOUT = raw as unknown as TownLayout;

/** Smooth road centreline: a centripetal Catmull-Rom spline through the listed points. */
export function sampleRoad(points: XZ[], perSegment = 16): XZ[] {
  if (points.length < 3) return points.map((p) => [p[0], p[1]] as XZ);
  const curve = new CatmullRomCurve3(points.map(([x, z]) => new Vector3(x, 0, z)), false, "centripetal");
  const n = (points.length - 1) * perSegment;
  const out: XZ[] = [];
  for (let i = 0; i <= n; i++) {
    const p = curve.getPoint(i / n);
    out.push([p.x, p.z]);
  }
  return out;
}

export const ROADS_SAMPLED = LAYOUT.roads.map((r) => ({ road: r, pts: sampleRoad(r.points) }));

// ---- path graph ---------------------------------------------------------------------------------

export interface PathResult {
  length: number;
  points: XZ[]; // sampled polyline from the first node to the last
  roads: string[]; // road ids walked, in order
}

const polyLength = (pts: XZ[]) => {
  let l = 0;
  for (let i = 1; i < pts.length; i++) l += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  return l;
};

interface Edge {
  to: string;
  road: string;
  pts: XZ[];
  len: number;
}
const ADJ = (() => {
  const adj = new Map<string, Edge[]>();
  const add = (k: string, e: Edge) => (adj.get(k) ?? adj.set(k, []).get(k)!).push(e);
  for (const { road, pts } of ROADS_SAMPLED) {
    const len = polyLength(pts);
    add(road.from, { to: road.to, road: road.id, pts, len });
    add(road.to, { to: road.from, road: road.id, pts: [...pts].reverse(), len });
  }
  return adj;
})();

/** Shortest walk between two nodes over all roads (every road is walkable both ways). */
export function shortestPath(from: string, to: string): PathResult | null {
  const dist = new Map<string, number>([[from, 0]]);
  const prev = new Map<string, { node: string; e: Edge }>();
  const todo = new Set<string>([from]);
  while (todo.size) {
    let u = "";
    for (const n of todo) if (u === "" || dist.get(n)! < dist.get(u)!) u = n;
    todo.delete(u);
    if (u === to) break;
    for (const e of ADJ.get(u) ?? []) {
      const nd = dist.get(u)! + e.len;
      if (nd < (dist.get(e.to) ?? Infinity)) {
        dist.set(e.to, nd);
        prev.set(e.to, { node: u, e });
        todo.add(e.to);
      }
    }
  }
  if (!dist.has(to)) return null;
  const legs: Edge[] = [];
  for (let at = to; at !== from; at = prev.get(at)!.node) legs.unshift(prev.get(at)!.e);
  const points: XZ[] = [];
  for (const l of legs) points.push(...(points.length ? l.pts.slice(1) : l.pts));
  return { length: dist.get(to)!, points: points.length ? points : [LAYOUT.nodes[from]], roads: legs.map((l) => l.road) };
}

/** The zone that shows DAG stage `stage` (1-based); stages beyond the last zone merge into it. */
export function zoneForStage(stage: number): ZoneSpec {
  const zs = LAYOUT.zones;
  return zs[Math.min(Math.max(stage, 1), zs.length) - 1];
}

/** Where a block's name label floats: just past the block's outer edge (away from the square), so
 * from the camera it sits below the near blocks and above the far ones, never over a cottage. */
export function zoneLabelAt(z: { x: number; z: number; d: number }): [number, number, number] {
  return [z.x, 0.3, z.z + Math.sign(z.z) * (z.d / 2 + 0.35)];
}
