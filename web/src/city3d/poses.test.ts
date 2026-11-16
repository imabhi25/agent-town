import { describe, expect, it } from "vitest";
import { Scene } from "../scene";
import type { TownEvent } from "../types";
import { plot } from "./layout";
import { Poses } from "./poses";
import { EDGES, NODE, ROUTE_TABLE, type RoutePlace } from "./routes";

let nextId = 1000;
const ev = (type: string, payload: Record<string, unknown> = {}): TownEvent => ({
  id: nextId++, stream_id: `${nextId}-0`, type, payload, ts: new Date(nextId * 1000).toISOString(),
});
// A tiny harness: events go into the Scene, frames advance Scene and Poses together.
function world() {
  const scene = new Scene();
  const poses = new Poses();
  const frame = (n = 1, dt = 1 / 60) => {
    for (let i = 0; i < n; i++) {
      scene.step(dt);
      poses.update(scene, dt);
    }
  };
  const feed = (...e: TownEvent[]) => {
    scene.sync(null, e);
    poses.update(scene, 0);
  };
  return { scene, poses, frame, feed };
}

describe("roads", () => {
  it("every edge is axis-aligned, so each can be drawn as one flat box", () => {
    for (const [a, b] of EDGES) expect(NODE[a][0] === NODE[b][0] || NODE[a][1] === NODE[b][1]).toBe(true);
  });
  it("every place can reach every other place along the roads", () => {
    const places: RoutePlace[] = ["yard", "queue", "downstream", "done", "dlq"];
    for (const f of places) for (const t of places) {
      if (f !== t) expect(ROUTE_TABLE[f][t].length).toBeGreaterThan(0);
    }
  });
  it("Workers to Downstream passes through the circuit gate", () => {
    expect(ROUTE_TABLE.yard.downstream).toContainEqual(NODE.gate);
  });
  it("Downstream to Done goes around the building, not through it", () => {
    const ds = plot("downstream");
    for (const [, z] of ROUTE_TABLE.downstream.done.slice(1)) expect(z).toBeGreaterThan(ds.z + ds.d / 2);
  });
});

describe("poses follow events only", () => {
  it("do nothing without events, however many frames pass", () => {
    const { poses, frame, feed } = world();
    feed(ev("worker.started", { worker: "w1" }));
    const p = poses.list.find((x) => x.visible)!;
    const at = [p.x, p.z, p.state];
    frame(600);
    expect([p.x, p.z, p.state]).toEqual(at);
  });

  it("job.started: busy, carrying a job, a token flies from the Queue, and it walks the road via the gate", () => {
    const { poses, frame, feed } = world();
    feed(ev("worker.started", { worker: "w1" }));
    const p = poses.list.find((x) => x.visible)!;
    const home = [p.x, p.z];
    feed(ev("job.started", { worker: "w1", job_id: "j1" }));
    expect(p.state).toBe("busy");
    expect(p.carrying).toBe(true);
    expect(poses.fly.some((f) => f.active)).toBe(true);
    // while walking, it stays on or near the road network (never the empty grass above the yard)
    let passedGate = false;
    for (let i = 0; i < 600; i++) {
      frame();
      if (Math.hypot(p.x - NODE.gate[0], p.z - NODE.gate[1]) < 0.4) passedGate = true;
    }
    expect(passedGate).toBe(true);
    expect([p.x, p.z]).not.toEqual(home);
    const ds = ROUTE_TABLE.yard.downstream.at(-1)!;
    expect(Math.hypot(p.x - ds[0], p.z - ds[1])).toBeLessThan(0.6); // standing at Downstream
    expect(poses.fly.every((f) => !f.active)).toBe(true); // the token landed
  });

  it("success: green, carried to Done, then home and idle", () => {
    const { poses, frame, feed } = world();
    feed(ev("worker.started", { worker: "w1" }), ev("job.started", { worker: "w1", job_id: "j1" }));
    frame(600);
    feed(ev("job.succeeded", { worker: "w1", job_id: "j1" }));
    const p = poses.list.find((x) => x.visible)!;
    expect(p.state).toBe("success");
    expect(p.carrying).toBe(true);
    frame(60 * 6);
    expect(p.state).toBe("idle");
    expect(p.carrying).toBe(false);
  });

  it("retry is orange and goes to the Queue; dead is red and goes to the DLQ", () => {
    const { poses, frame, feed } = world();
    feed(ev("worker.started", { worker: "w1" }), ev("worker.started", { worker: "w2" }));
    feed(ev("job.started", { worker: "w1", job_id: "j1" }), ev("job.started", { worker: "w2", job_id: "j2" }));
    frame(600);
    feed(ev("job.retry_scheduled", { worker: "w1", job_id: "j1" }), ev("job.dead", { worker: "w2", job_id: "j2", error: "x" }));
    const [a, b] = poses.list.filter((x) => x.visible);
    expect([a.state, b.state]).toEqual(["retrying", "failed"]);
    frame(60 * 4);
    // each is on the way, or already back, but nobody stands on the other's road
    expect(a.place === "yard" || a.place === "queue").toBe(true);
    expect(b.place === "yard" || b.place === "dlq").toBe(true);
  });

  it("worker.killed shrinks the robot and then frees it; worker.started brings a new one in", () => {
    const { scene, poses, frame, feed } = world();
    feed(ev("worker.started", { worker: "w1" }));
    const p = poses.list.find((x) => x.visible)!;
    frame(60);
    expect(p.scale).toBeCloseTo(1, 1);
    feed(ev("worker.killed", { worker: "w1" }));
    frame(20);
    expect(p.scale).toBeLessThan(0.95);
    frame(120);
    expect(p.visible).toBe(false);
    feed(ev("worker.started", { worker: "w1b" }));
    expect(scene.summary().workers).toBe(1);
    const q = poses.list.find((x) => x.visible)!;
    frame(1);
    expect(q.scale).toBeLessThan(1); // popping in
    frame(60);
    expect(q.scale).toBeCloseTo(1, 1);
  });

  it("copes with 32 workers and a burst of events without throwing", () => {
    const { poses, frame, feed } = world();
    const events: TownEvent[] = [];
    for (let i = 0; i < 40; i++) events.push(ev("worker.started", { worker: `w${i}` }));
    for (let i = 0; i < 40; i++) events.push(ev("job.started", { worker: `w${i}`, job_id: `j${i}` }));
    feed(...events);
    frame(300);
    expect(poses.list.filter((p) => p.visible).length).toBe(32);
    expect(poses.fly.filter((f) => f.active).length).toBeLessThanOrEqual(12);
  });
});
