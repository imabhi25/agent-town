// Roads as a tiny graph, so a worker walks ALONG the paths instead of across the grass.
// Pure data and maths (no three.js): unit tested without WebGL.
import { GATE_POS, plot } from "./layout";

export type RoutePlace = "yard" | "queue" | "downstream" | "done" | "dlq";
type XZ = [number, number];

const yard = plot("yard"), queue = plot("queue"), ds = plot("downstream"), done = plot("done"), dlq = plot("dlq"), planner = plot("planner");
const roadZ = GATE_POS[1];

export const NODE: Record<string, XZ> = {
  plannerS: [planner.x, planner.z + planner.d / 2 + 0.1],
  queueN: [queue.x, queue.z - queue.d / 2 - 0.1],
  queueE: [queue.x + queue.w / 2 + 0.1, queue.z],
  yardW: [yard.x - yard.w / 2 - 0.2, queue.z],
  yardE: [yard.x + yard.w / 2 + 0.2, roadZ],
  yardS: [yard.x, yard.z + yard.d / 2 + 0.2],
  gate: [GATE_POS[0], roadZ],
  dsW: [ds.x - ds.w / 2 - 0.15, roadZ],
  dsW2: [ds.x - ds.w / 2 - 0.15, ds.z + ds.d / 2 + 0.4],
  dsF: [ds.x, ds.z + ds.d / 2 + 0.4],
  doneN: [done.x, done.z - done.d / 2 - 0.2],
  dlqN: [dlq.x, dlq.z - dlq.d / 2 - 0.1],
};

// Every edge is horizontal or vertical, so it can be drawn as one flat road box.
export const EDGES: [string, string][] = [
  ["plannerS", "queueN"],
  ["yardW", "queueE"],
  ["yardE", "gate"],
  ["gate", "dsW"],
  ["dsW", "dsW2"],
  ["dsW2", "dsF"],
  ["dsF", "doneN"],
  ["yardS", "dlqN"],
];

const ADJ = new Map<string, string[]>();
for (const [a, b] of EDGES) {
  ADJ.set(a, [...(ADJ.get(a) ?? []), b]);
  ADJ.set(b, [...(ADJ.get(b) ?? []), a]);
}

function bfs(from: string, to: string): string[] {
  const prev = new Map<string, string | null>([[from, null]]);
  const q = [from];
  for (let i = 0; i < q.length; i++) {
    if (q[i] === to) break;
    for (const n of ADJ.get(q[i]) ?? []) if (!prev.has(n)) (prev.set(n, q[i]), q.push(n));
  }
  const out: string[] = [];
  for (let at: string | null | undefined = to; at; at = prev.get(at)) out.unshift(at);
  return out;
}

const ANCHOR = { queue: "queueE", downstream: "dsW", done: "doneN", dlq: "dlqN" } as const;
const YARD_EXIT = { queue: "yardW", downstream: "yardE", done: "yardE", dlq: "yardS" } as const;
const PLACES: RoutePlace[] = ["yard", "queue", "downstream", "done", "dlq"];

/** Road points to follow from one place to another (the yard end is the worker's own slot). */
export function roadPoints(from: RoutePlace, to: RoutePlace): XZ[] {
  if (from === to) return [];
  const a = from === "yard" ? YARD_EXIT[to as keyof typeof YARD_EXIT] : ANCHOR[from];
  const b = to === "yard" ? YARD_EXIT[from as keyof typeof YARD_EXIT] : ANCHOR[to as keyof typeof ANCHOR];
  return bfs(a, b).map((n) => NODE[n]);
}

export const ROUTE_TABLE: Record<RoutePlace, Record<RoutePlace, XZ[]>> = Object.fromEntries(
  PLACES.map((f) => [f, Object.fromEntries(PLACES.map((t) => [t, roadPoints(f, t)]))]),
) as never;
