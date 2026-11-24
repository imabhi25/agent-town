import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { initialState, reduce } from "../store";
import type { TownEvent, Trace } from "../types";
import { deriveTown, stagesOf } from "./townModel";

const trace = (name: string): Trace => JSON.parse(readFileSync(`public/traces/${name}.json`, "utf8"));

let nextId = 1;
const ev = (type: string, payload: Record<string, unknown>, ts = "2026-01-01T00:00:00Z"): TownEvent => ({
  id: nextId++, stream_id: `${nextId}-0`, type, payload, ts,
});
const G = { goal_id: "g1" };
const created = (key: string, deps: string[] = [], extra: Record<string, unknown> = {}) =>
  ev("task.created", { ...G, key, name: key, task_id: `id-${key}`, depends_on: deps.map((d) => `id-${d}`), needs_approval: false, ...extra });
const store = (events: TownEvent[]) => reduce(initialState, { type: "events", events }).state;

const DIAMOND = [created("t1"), created("t2", ["t1"]), created("t3", ["t1"]), created("t4", ["t2", "t3"])];

describe("stagesOf", () => {
  it("is 1 + the deepest dependency", () => {
    const s = stagesOf([
      { id: "a", depends_on: [] }, { id: "b", depends_on: ["a"] }, { id: "c", depends_on: ["a", "b"] },
    ]);
    expect([s.get("a"), s.get("b"), s.get("c")]).toEqual([1, 2, 3]);
  });
  it("survives a cycle and an unknown dependency", () => {
    const s = stagesOf([{ id: "a", depends_on: ["b"] }, { id: "b", depends_on: ["a"] }, { id: "c", depends_on: ["zzz"] }]);
    expect(s.size).toBe(3);
    expect(s.get("c")).toBe(2);
  });
});

describe("deriveTown", () => {
  it("is empty before any goal", () => {
    const m = deriveTown(store([ev("worker.started", { worker: "w1" })]));
    expect(m.goalStatus).toBe("none");
    expect(m.tasks).toEqual([]);
    expect(m.workers.map((w) => w.id)).toEqual(["w1"]);
  });

  it("puts the diamond in three zones of 1, 2, 1 tasks, each in its own slot", () => {
    const m = deriveTown(store(DIAMOND));
    expect(m.tasks.map((t) => [t.key, t.zone, t.slot])).toEqual([["t1", 1, 0], ["t2", 2, 0], ["t3", 2, 1], ["t4", 3, 0]]);
    expect(m.zones.map((z) => z.tasks)).toEqual([1, 2, 1]);
    expect(m.tasks.every((t) => t.status === "planned")).toBe(true);
  });

  it("merges stages past the last zone into it", () => {
    const chain = ["a", "b", "c", "d", "e", "f", "g", "h"].map((k, i, a) => created(k, i ? [a[i - 1]] : []));
    const m = deriveTown(store(chain));
    expect(Math.max(...m.tasks.map((t) => t.zone))).toBe(6);
    expect(m.tasks.filter((t) => t.zone === 6).length).toBe(3);
  });

  it("follows a job: queued, running, then built", () => {
    const base = [...DIAMOND, ev("task.queued", { ...G, task_id: "id-t1", job_id: "j1" })];
    expect(deriveTown(store(base)).tasks[0].status).toBe("queued");
    const run = [...base, ev("job.started", { job_id: "j1", worker: "w1" })];
    const m = deriveTown(store(run));
    expect(m.tasks[0]).toMatchObject({ status: "running", worker: "w1" });
    expect(m.workers[0]).toMatchObject({ id: "w1", taskId: "id-t1" });
    const done = [...run, ev("job.succeeded", { job_id: "j1", worker: "w1" }), ev("task.succeeded", { ...G, task_id: "id-t1" })];
    const d = deriveTown(store(done));
    expect(d.tasks[0].status).toBe("succeeded");
    expect(d.workers[0].taskId).toBeNull();
    expect(d.zones[0]).toEqual({ zone: 1, tasks: 1, built: 1 });
  });

  it("shows a retry with its countdown, then running again", () => {
    const e = [
      ...DIAMOND,
      ev("task.queued", { ...G, task_id: "id-t1", job_id: "j1" }),
      ev("job.started", { job_id: "j1", worker: "w1" }),
      ev("job.retry_scheduled", { job_id: "j1", worker: "w1", attempt: 1, delay: 2 }, "2026-01-01T00:00:10Z"),
    ];
    const t = deriveTown(store(e)).tasks[0];
    expect(t).toMatchObject({ status: "retrying", attempt: 1 });
    expect(t.retryAtMs).toBe(Date.parse("2026-01-01T00:00:12Z"));
    const again = deriveTown(store([...e, ev("job.started", { job_id: "j1", worker: "w2" }, "2026-01-01T00:00:13Z")])).tasks[0];
    expect(again).toMatchObject({ status: "running", worker: "w2", retryAtMs: null });
  });

  it("dead job leaves rubble and replay rebuilds it", () => {
    const e = [
      ...DIAMOND,
      ev("task.queued", { ...G, task_id: "id-t1", job_id: "j1" }),
      ev("job.dead", { job_id: "j1", worker: "w1", error: "x" }),
    ];
    const m = deriveTown(store(e));
    expect(m.tasks[0].status).toBe("dead");
    expect(m.deadJobs).toBe(1);
    const r = deriveTown(store([...e, ev("job.replayed", { job_id: "j1" })]));
    expect(r.tasks[0].status).toBe("queued");
    expect(r.deadJobs).toBe(0);
  });

  it("hands a killed worker's job to the new worker (job.recovered)", () => {
    const e = [
      ...DIAMOND,
      ev("task.queued", { ...G, task_id: "id-t1", job_id: "j1" }),
      ev("job.started", { job_id: "j1", worker: "w1" }),
      ev("worker.killed", { worker: "w1" }),
      ev("worker.started", { worker: "w2" }),
      ev("job.recovered", { job_id: "j1", new_worker: "w2", previous_worker: "w1" }),
    ];
    const m = deriveTown(store(e));
    expect(m.tasks[0]).toMatchObject({ status: "running", worker: "w2" });
    expect(m.workers.find((w) => w.id === "w1")?.gone).toBe(true);
    expect(m.workers.find((w) => w.id === "w2")?.taskId).toBe("id-t1");
  });

  it("approval gate and circuit barrier", () => {
    const e = [...DIAMOND, ev("task.awaiting_approval", { ...G, task_id: "id-t4", name: "t4" }), ev("circuit.opened", { provider: "llm" })];
    const m = deriveTown(store(e));
    expect(m.tasks[3].status).toBe("awaiting_approval");
    expect(m.circuitOpen).toBe(true);
    const ok = deriveTown(store([...e, ev("circuit.closed", { provider: "llm" }), ev("task.approved", { ...G, task_id: "id-t4" })]));
    expect(ok.circuitOpen).toBe(false);
    expect(ok.tasks[3].status).toBe("planned");
    expect(deriveTown(store([...e.slice(0, -1), ev("circuit.opened", { provider: "planner" })])).circuitOpen).toBe(false);
  });

  it("ignores jobs that belong to no task (planner, flood)", () => {
    const m = deriveTown(store([...DIAMOND, ev("job.started", { job_id: "planner-job", worker: "w1" })]));
    expect(m.tasks.every((t) => t.status === "planned")).toBe(true);
    expect(m.workers[0]).toMatchObject({ id: "w1", taskId: null, jobId: "planner-job" });
  });

  it("is unchanged by duplicate and out-of-order delivery (the store sorts and dedupes)", () => {
    const e = [...DIAMOND, ev("task.queued", { ...G, task_id: "id-t1", job_id: "j1" }), ev("job.started", { job_id: "j1", worker: "w1" })];
    const shuffled = [...e].reverse().concat(e[2]);
    expect(deriveTown(store(shuffled))).toEqual(deriveTown(store(e)));
  });

  it("only shows the latest goal", () => {
    const old = ev("task.created", { goal_id: "g0", key: "t1", name: "old", task_id: "old-1", depends_on: [], needs_approval: false }, "2025-12-31T00:00:00Z");
    const m = deriveTown(store([old, ...DIAMOND]));
    expect(m.goalId).toBe("g1");
    expect(m.tasks).toHaveLength(4);
  });
});

describe("recorded traces", () => {
  const run = (name: string) => {
    const t = trace(name);
    return deriveTown(store(t.events));
  };
  const SHAPES: Record<string, number[]> = {
    "happy-path": [1, 2, 1],
    retries: [1, 2, 1], // the recorded diamond; fixed-planner retries preset has its own shape
    "chaos-recovery": [1, 2, 1],
    "dlq-replay-approval": [1, 2, 1],
  };
  for (const [name, shape] of Object.entries(SHAPES)) {
    it(`${name}: diamond layout and the goal ends built`, () => {
      const m = run(name);
      expect(m.zones.map((z) => z.tasks)).toEqual(shape);
      expect(m.goalStatus).toBe("succeeded");
      expect(m.tasks.every((t) => t.status === "succeeded")).toBe(true);
      expect(m.finishedMs! - m.startedMs!).toBeGreaterThan(0);
    });
  }
});
