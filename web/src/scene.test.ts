import { describe, expect, it } from "vitest";
import { BUILDINGS, MAX_WORKERS, Scene, homeOf, placeOf } from "./scene";
import type { Snapshot, TownEvent } from "./types";

let nextId = 1;
const ev = (type: string, payload: Record<string, unknown> = {}): TownEvent => ({
  id: nextId++, stream_id: `${nextId}-0`, type, payload, ts: new Date(nextId * 1000).toISOString(),
});
const snap = (over: Partial<Snapshot> = {}): Snapshot => ({
  type: "snapshot", goals: [], tasks: [], workers: [], queue: { depth: 0, succeeded: 0, dead: 0 },
  stats: {}, events: [], last_event_id: "0-0", ...over,
});
const run = (s: Scene, seconds: number) => { for (let i = 0; i < seconds * 60; i++) s.step(1 / 60); };
const feed = (s: Scene, ...events: TownEvent[]) => s.sync(null, events);

describe("only events move the scene", () => {
  it("does nothing on its own, however many frames pass", () => {
    const s = new Scene();
    feed(s, ev("worker.started", { worker: "w1" }));
    const before = JSON.stringify(s.summary());
    const w = s.worker("w1")!;
    const pos = [w.x, w.y, w.state];
    run(s, 30);
    expect(JSON.stringify(s.summary())).toBe(before);
    expect([w.x, w.y, w.state]).toEqual(pos);
  });
  it("applying the same events twice changes nothing (idempotent sync)", () => {
    const s = new Scene();
    const events = [ev("goal.created"), ev("task.queued"), ev("chaos.flood", { count: 5 })];
    s.sync(null, events);
    s.sync(null, events);
    expect(s.queueDepth).toBe(7);
  });
});

describe("worker lifecycle", () => {
  it("invalidates the overlay when known workers change job counters", () => {
    const s = new Scene();
    feed(s, ev("worker.started", { worker: "w1" }));
    const changes = [
      ev("job.created", { job_id: "j1" }),
      ev("job.started", { worker: "w1", job_id: "j1" }),
      ev("job.succeeded", { worker: "w1", job_id: "j1" }),
    ];

    for (const event of changes) {
      const version = s.version;
      feed(s, event);
      expect(s.version).toBeGreaterThan(version);
    }
    expect(s.summary()).toMatchObject({ queue: 0, busy: 0, done: 1 });
  });
  it("job.started: worker turns busy and walks to Downstream", () => {
    const s = new Scene();
    feed(s, ev("worker.started", { worker: "w1" }), ev("job.started", { worker: "w1", job_id: "j1" }));
    const w = s.worker("w1")!;
    expect(w.state).toBe("busy");
    expect(w.jobId).toBe("j1");
    run(s, 3);
    const [dx, dy] = placeOf("downstream", w.slot);
    expect([w.x, w.y]).toEqual([dx, dy]);
    expect(w.state).toBe("busy"); // stays busy until a result event arrives
  });
  it("job.succeeded: green, +1 done, walks to Done, then home and idle", () => {
    const s = new Scene();
    feed(s, ev("job.started", { worker: "w1", job_id: "j1" }), ev("job.succeeded", { worker: "w1", job_id: "j1" }));
    const w = s.worker("w1")!;
    expect(w.state).toBe("success");
    expect(s.done).toBe(1);
    run(s, 0.2);
    expect(w.state).toBe("success");
    run(s, 5);
    expect(w.state).toBe("idle");
    expect([w.x, w.y]).toEqual(homeOf(w.slot));
  });
  it("job.retry_scheduled: orange, back to the Queue, depth +1", () => {
    const s = new Scene();
    feed(s, ev("job.started", { worker: "w1", job_id: "j1" }), ev("job.retry_scheduled", { worker: "w1", job_id: "j1" }));
    expect(s.worker("w1")!.state).toBe("retrying");
    expect(s.queueDepth).toBe(1);
    expect(s.worker("w1")!.ty).toBeGreaterThan(BUILDINGS.queue.y);
  });
  it("job.dead: red, to the DLQ, dead job listed with its error", () => {
    const s = new Scene();
    feed(s, ev("job.dead", { worker: "w1", job_id: "j9", error: "boom" }));
    expect(s.worker("w1")!.state).toBe("failed");
    expect(s.dead).toBe(1);
    expect(s.deadJobs).toEqual([{ jobId: "j9", error: "boom" }]);
    run(s, 5);
    expect(s.worker("w1")!.state).toBe("idle");
  });
  it("job.replayed: leaves the DLQ list and re-enters the queue", () => {
    const s = new Scene();
    feed(s, ev("job.dead", { worker: "w1", job_id: "j9" }), ev("job.replayed", { job_id: "j9" }));
    expect(s.dead).toBe(0);
    expect(s.deadJobs).toEqual([]);
    expect(s.queueDepth).toBe(1);
  });
  it("job.created queues a job; chaos.config records the failure rate", () => {
    const s = new Scene();
    feed(s, ev("job.created", { job_id: "x" }), ev("chaos.config", { failure_rate: 0.6 }));
    expect(s.queueDepth).toBe(1);
    expect(s.failureRate).toBe(0.6);
  });
  it("job.started takes a job off the queue (never below zero)", () => {
    const s = new Scene();
    feed(s, ev("job.started", { worker: "w1", job_id: "a" }));
    expect(s.queueDepth).toBe(0);
    feed(s, ev("chaos.flood", { count: 3 }), ev("job.started", { worker: "w2", job_id: "b" }));
    expect(s.queueDepth).toBe(2);
  });
  it("worker.killed: fades out, then the slot is reusable by a replacement", () => {
    const s = new Scene();
    feed(s, ev("worker.started", { worker: "w1" }));
    const slot = s.worker("w1")!.slot;
    feed(s, ev("worker.killed", { worker: "w1" }));
    expect(s.summary().workers).toBe(0);
    expect(s.pool.filter((w) => w.active)).toHaveLength(1); // still fading
    run(s, 1);
    expect(s.pool.filter((w) => w.active)).toHaveLength(0);
    feed(s, ev("worker.started", { worker: "w1b" }));
    expect(s.worker("w1b")!.slot).toBe(slot);
  });
  it("shows at most MAX_WORKERS sprites without crashing", () => {
    const s = new Scene();
    for (let i = 0; i < MAX_WORKERS + 5; i++) feed(s, ev("worker.started", { worker: `w${i}` }));
    expect(s.summary().workers).toBe(MAX_WORKERS);
  });
});

describe("circuit gate", () => {
  it("gate shuts on circuit.opened and reopens on circuit.closed (eased)", () => {
    const s = new Scene();
    expect(s.gate).toBe(0);
    feed(s, ev("circuit.opened", { provider: "gemini" }));
    expect(s.summary().circuit).toBe("tripped");
    run(s, 0.05);
    expect(s.gate).toBeGreaterThan(0);
    expect(s.gate).toBeLessThan(1);
    run(s, 2);
    expect(s.gate).toBe(1);
    feed(s, ev("circuit.closed", { provider: "gemini" }));
    run(s, 2);
    expect(s.gate).toBe(0);
    expect(s.summary().circuit).toBe("ok");
  });
  it("stays shut while any provider's circuit is open", () => {
    const s = new Scene();
    feed(s, ev("circuit.opened", { provider: "a" }), ev("circuit.opened", { provider: "b" }), ev("circuit.closed", { provider: "a" }));
    expect(s.summary().circuit).toBe("tripped");
  });
});

describe("approvals", () => {
  it("shows a card on task.awaiting_approval and removes it when decided", () => {
    const s = new Scene();
    feed(s, ev("task.awaiting_approval", { goal_id: "g", task_id: "t1", name: "Book hotel" }));
    expect(s.approvals).toEqual([{ taskId: "t1", goalId: "g", name: "Book hotel" }]);
    feed(s, ev("task.approved", { goal_id: "g", task_id: "t1" }));
    expect(s.approvals).toEqual([]);
  });
  it("does not duplicate a card for a repeated event", () => {
    const s = new Scene();
    const e = { goal_id: "g", task_id: "t1", name: "x" };
    feed(s, ev("task.awaiting_approval", e), ev("task.awaiting_approval", e));
    expect(s.approvals).toHaveLength(1);
  });
});

describe("store reset (a recording restarts)", () => {
  it("applies the same events again after the store epoch changes", () => {
    const s = new Scene();
    const sn = snap();
    const e = ev("task.awaiting_approval", { goal_id: "g", task_id: "t1", name: "x" });
    s.sync(sn, [e], 0);
    expect(s.approvals).toHaveLength(1);
    // TownStore.reset() bumps the epoch; the replay then feeds the SAME snapshot object and events.
    s.sync(sn, [], 1);
    expect(s.approvals).toEqual([]);
    s.sync(sn, [e], 1);
    expect(s.approvals).toHaveLength(1);
  });
  it("keeps counting when the epoch has not changed", () => {
    const s = new Scene();
    const e = ev("job.succeeded", { worker: "w1", job_id: "k" });
    s.sync(null, [e], 0);
    s.sync(null, [e], 0);
    expect(s.done).toBe(1);
  });
});

describe("snapshots", () => {
  it("rebase sets counters, workers, dead jobs and pending approvals", () => {
    const s = new Scene();
    s.sync(
      snap({
        queue: { depth: 4, succeeded: 10, dead: 2 },
        workers: [{ id: "w1", job_id: "j1" }, { id: "w2", job_id: null }],
        dead_jobs: [{ job_id: "d1", type: "llm.mock" }],
        tasks: [{ id: "t1", goal_id: "g", name: "Approve me", status: "awaiting_approval" }, { id: "t2", status: "queued" }],
      }),
      [],
    );
    expect(s.summary()).toMatchObject({ workers: 2, busy: 1, queue: 4, done: 10, dead: 2, approvals: 1 });
    expect(s.deadJobs).toEqual([{ jobId: "d1", type: "llm.mock" }]);
  });
  it("does not double count events that are already inside the snapshot", () => {
    const s = new Scene();
    const inside = ev("job.succeeded", { worker: "w1", job_id: "j" });
    s.sync(snap({ queue: { depth: 0, succeeded: 5, dead: 0 }, events: [inside] }), [inside]);
    expect(s.done).toBe(5);
    const later = ev("job.succeeded", { worker: "w1", job_id: "k" });
    s.sync(null, [inside, later]);
    expect(s.done).toBe(6);
  });
  it("a new snapshot drops workers that are gone", () => {
    const s = new Scene();
    s.sync(snap({ workers: [{ id: "a", job_id: null }, { id: "b", job_id: null }] }), []);
    s.sync(snap({ workers: [{ id: "b", job_id: null }] }), []);
    expect(s.worker("a")).toBeUndefined();
    expect(s.summary().workers).toBe(1);
  });
});

describe("performance", () => {
  it("applies 20,000 mixed events and 600 frames quickly, reusing the same worker objects", () => {
    const s = new Scene();
    const pool = [...s.pool];
    const events: TownEvent[] = [];
    for (let i = 0; i < 20000; i++) {
      const w = `w${i % 8}`;
      events.push(ev(["job.started", "job.succeeded", "job.retry_scheduled", "job.dead"][i % 4], { worker: w, job_id: `j${i}` }));
    }
    const t0 = performance.now();
    for (let i = 0; i < events.length; i += 100) s.sync(null, events.slice(0, i + 100));
    for (let i = 0; i < 600; i++) s.step(1 / 60);
    expect(performance.now() - t0).toBeLessThan(1500);
    expect(s.pool.every((w, i) => w === pool[i])).toBe(true);
  });
});
