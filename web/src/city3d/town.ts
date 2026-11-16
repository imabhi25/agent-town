// The town as data. docs/reference/town-layout.json is the single source: building footprints and
// rotations, road centrelines (with a type: normal / retry / dead), the path graph (a road is an
// edge between two nodes), park islands, props and the two gates. Everything here is pure maths
// (no WebGL), so the layout is unit tested: see town.test.ts.
import { CatmullRomCurve3, Vector3 } from "three";
import raw from "../../../docs/reference/town-layout.json";

export type RoadType = "normal" | "retry" | "dead";
export type PropKind = "tree" | "bush" | "rock" | "flower" | "bench" | "lamp";
export type BuildingId = "planner" | "queue" | "workers" | "downstream" | "done" | "dlq";
export type XZ = [number, number];

export interface BuildingSpec {
  id: BuildingId;
  kind: BuildingId;
  x: number;
  z: number;
  rot: number; // degrees about the vertical axis; local +z is the front
  w: number;
  d: number;
  doors: Record<string, XZ>; // node id -> local offset of that door
}
export interface RoadSpec {
  id: string;
  type: RoadType;
  width: number;
  from: string;
  to: string;
  interior: boolean; // a path through a building: walkable, not drawn
  points: XZ[];
}
export interface IslandSpec {
  id: string;
  x: number;
  z: number;
  r: number;
}
export interface PropSpec {
  kind: PropKind;
  x: number;
  z: number;
  s: number;
  c?: number;
  rot?: number;
}
export interface GateSpec {
  x: number;
  z: number;
  rot: number;
  on: string;
}
export interface TownLayout {
  version: number;
  board: { w: number; d: number; corner: number };
  buildings: BuildingSpec[];
  nodes: Record<string, XZ>;
  roads: RoadSpec[];
  islands: IslandSpec[];
  gates: { circuit: GateSpec; approval: GateSpec };
  props: PropSpec[];
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

/** Shortest walk between two nodes over all roads (walkable both ways; arrows show job flow). */
export function shortestPath(from: string, to: string): PathResult | null {
  const adj = new Map<string, { to: string; road: string; pts: XZ[]; len: number }[]>();
  for (const { road, pts } of ROADS_SAMPLED) {
    const len = polyLength(pts);
    (adj.get(road.from) ?? adj.set(road.from, []).get(road.from)!).push({ to: road.to, road: road.id, pts, len });
    (adj.get(road.to) ?? adj.set(road.to, []).get(road.to)!).push({ to: road.from, road: road.id, pts: [...pts].reverse(), len });
  }
  const dist = new Map<string, number>([[from, 0]]);
  const prev = new Map<string, { node: string; e: { road: string; pts: XZ[] } }>();
  const todo = new Set<string>([from]);
  while (todo.size) {
    let u = "";
    for (const n of todo) if (u === "" || dist.get(n)! < dist.get(u)!) u = n;
    todo.delete(u);
    if (u === to) break;
    for (const e of adj.get(u) ?? []) {
      const nd = dist.get(u)! + e.len;
      if (nd < (dist.get(e.to) ?? Infinity)) {
        dist.set(e.to, nd);
        prev.set(e.to, { node: u, e });
        todo.add(e.to);
      }
    }
  }
  if (!dist.has(to)) return null;
  const legs: { road: string; pts: XZ[] }[] = [];
  for (let at = to; at !== from; at = prev.get(at)!.node) legs.unshift(prev.get(at)!.e);
  const points: XZ[] = [];
  for (const l of legs) points.push(...(points.length ? l.pts.slice(1) : l.pts));
  return { length: dist.get(to)!, points: points.length ? points : [LAYOUT.nodes[from]], roads: legs.map((l) => l.road) };
}

/** World position of a building door (local offset rotated by the building's rotation). */
export function doorWorld(b: BuildingSpec, local: XZ): XZ {
  const a = (b.rot * Math.PI) / 180;
  return [b.x + local[0] * Math.cos(a) + local[1] * Math.sin(a), b.z - local[0] * Math.sin(a) + local[1] * Math.cos(a)];
}
