// Phase B model: the event store -> what the town shows. Pure (no three.js, no React, no clock), so
// every rule is unit tested in townModel.test.ts and a recorded trace and the live feed give the
// identical town.
//
// Why replay from the event list instead of keeping state per event: the store already dedupes and
// sorts (ts, id) and keeps at most 500 events, so folding over them is cheap, and out-of-order or
// duplicate delivery can never leave this model in a state the store would not produce.
//
// What the events carry (see PLAN.md "What the event stream really carries"; one correction: depends_on holds task ids):
//  - a task has no zone field, so the zone is the DAG stage = 1 + deepest stage of its dependencies;
//  - job events carry job_id + worker; the job -> task link is only `task.queued {job_id, task_id}`;
//  - jobs with no task (planner job, flood jobs) belong to the Foreman, not a zone.
import { LAYOUT, zoneForStage } from "./town";
import type { StoreState } from "../store";
import type { TownEvent } from "../types";

export type BuildingStatus =
  | "planned" // ghost foundation
  | "queued" // job exists, no worker yet
  | "running" // scaffold up, a worker is on it
  | "retrying" // orange, counting down to the next attempt
  | "awaiting_approval" // waits at the approval gate
  | "succeeded" // building raised
  | "dead"; // rubble and a red beacon (DLQ)

export interface TaskView {
  id: string;
  key: string;
  name: string;
  stage: number; // DAG stage, 1-based
  zone: number; // zone index 1..zones (stages past the last zone merge into it)
  slot: number; // 0-based position inside the zone, in creation order
  status: BuildingStatus;
  jobId: string | null;
  worker: string | null;
  attempt: number; // last retry_scheduled attempt (0 = none yet)
  retryAtMs: number | null; // wall time (ms) the next attempt starts; the UI counts down to it
  needsApproval: boolean;
}

export interface WorkerView {
  id: string;
  taskId: string | null; // null = idle or on a task-less job
  jobId: string | null;
  gone: boolean; // killed
}

export interface TownModel {
  goalId: string | null;
  goalStatus: "none" | "running" | "succeeded";
  startedMs: number | null;
  finishedMs: number | null;
  tasks: TaskView[]; // current goal only, creation order
  zones: { zone: number; tasks: number; built: number }[];
  workers: WorkerView[];
  circuitOpen: boolean; // llm circuit: every barrier lowers
  deadJobs: number;
  retries: number; // job.retry_scheduled events for this goal's tasks
}

export const EMPTY_MODEL: TownModel = {
  goalId: null, goalStatus: "none", startedMs: null, finishedMs: null,
  tasks: [], zones: [], workers: [], circuitOpen: false, deadJobs: 0, retries: 0,
};

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const ms = (e: TownEvent) => Date.parse(e.ts);

/** Stage of each task id: 1 + the deepest stage among its dependencies (no dependencies = 1).
 * `depends_on` in task.created holds task ids (checked against the recorded traces), not keys. */
export function stagesOf(tasks: { id: string; depends_on: string[] }[]): Map<string, number> {
  const byKey = new Map(tasks.map((t) => [t.id, t]));
  const memo = new Map<string, number>();
  const visit = (k: string, seen: Set<string>): number => {
    const known = memo.get(k);
    if (known) return known;
    const t = byKey.get(k);
    if (!t || seen.has(k)) return 1; // unknown dependency or a cycle: treat as a root, never loop
    seen.add(k);
    const s = 1 + Math.max(0, ...t.depends_on.map((d) => visit(d, seen)));
    memo.set(k, s);
    return s;
  };
  for (const t of tasks) visit(t.id, new Set());
  return memo;
}

export function deriveTown(state: Pick<StoreState, "events">): TownModel {
  const events = state.events;
  // The current goal is the one whose task.created came last.
  let goalId: string | null = null;
  for (const e of events) if (e.type === "task.created") goalId = str(e.payload.goal_id);
  if (goalId === null) return deriveWorkersOnly(events);

  const tasks = new Map<string, TaskView>();
  const deps = new Map<string, string[]>();
  const jobToTask = new Map<string, string>();
  const workers = new Map<string, WorkerView>();
  let started: number | null = null;
  let finished: number | null = null;
  let goalStatus: TownModel["goalStatus"] = "none";
  let circuitOpen = false;
  const dead = new Set<string>();
  let retries = 0;

  const setWorker = (id: string, jobId: string | null) => {
    if (!id) return;
    const w = workers.get(id) ?? { id, taskId: null, jobId: null, gone: false };
    w.jobId = jobId;
    w.taskId = jobId ? (jobToTask.get(jobId) ?? null) : null;
    w.gone = false;
    workers.set(id, w);
  };
  const taskOfJob = (jobId: string) => {
    const id = jobToTask.get(jobId);
    return id ? tasks.get(id) : undefined;
  };

  for (const e of events) {
    const p = e.payload;
    const inGoal = str(p.goal_id) === goalId;
    switch (e.type) {
      case "task.created":
        if (!inGoal) break;
        tasks.set(str(p.task_id), {
          id: str(p.task_id), key: str(p.key), name: str(p.name), stage: 1, zone: 1, slot: 0,
          status: "planned", jobId: null, worker: null, attempt: 0, retryAtMs: null,
          needsApproval: p.needs_approval === true,
        });
        deps.set(str(p.task_id), Array.isArray(p.depends_on) ? (p.depends_on as string[]) : []);
        break;
      case "goal.started":
        if (inGoal) {
          goalStatus = "running";
          started = ms(e);
        }
        break;
      case "goal.succeeded":
        if (inGoal) {
          goalStatus = "succeeded";
          finished = ms(e);
        }
        break;
      case "task.queued": {
        const t = tasks.get(str(p.task_id));
        if (!t) break;
        jobToTask.set(str(p.job_id), t.id);
        t.jobId = str(p.job_id);
        if (t.status !== "succeeded") t.status = "queued";
        break;
      }
      case "task.awaiting_approval": {
        const t = tasks.get(str(p.task_id));
        if (t) t.status = "awaiting_approval";
        break;
      }
      case "task.approved": {
        const t = tasks.get(str(p.task_id));
        if (t && t.status === "awaiting_approval") t.status = "planned";
        break;
      }
      case "task.succeeded": {
        const t = tasks.get(str(p.task_id));
        if (t) {
          t.status = "succeeded";
          t.retryAtMs = null;
        }
        break;
      }
      case "task.failed":
      case "task.rejected":
      case "task.cancelled": {
        const t = tasks.get(str(p.task_id));
        if (t) t.status = "dead";
        break;
      }
      case "worker.started":
        setWorker(str(p.worker), null);
        break;
      case "worker.killed": {
        const w = workers.get(str(p.worker));
        if (w) {
          w.gone = true;
          w.jobId = null;
          w.taskId = null;
        }
        break;
      }
      case "job.started": {
        const t = taskOfJob(str(p.job_id));
        setWorker(str(p.worker), str(p.job_id));
        if (t && t.status !== "succeeded") {
          t.status = "running";
          t.worker = str(p.worker);
          t.retryAtMs = null;
        }
        break;
      }
      case "job.recovered": {
        // A killed worker's job was reclaimed: the new worker carries on the same building.
        const t = taskOfJob(str(p.job_id));
        setWorker(str(p.new_worker), str(p.job_id));
        if (t && t.status !== "succeeded") {
          t.status = "running";
          t.worker = str(p.new_worker);
        }
        break;
      }
      case "job.succeeded":
        setWorker(str(p.worker), null);
        break;
      case "job.retry_scheduled": {
        const t = taskOfJob(str(p.job_id));
        setWorker(str(p.worker), null);
        if (t) retries++;
        if (t && t.status !== "succeeded") {
          t.status = "retrying";
          t.worker = null;
          t.attempt = Number(p.attempt) || t.attempt + 1;
          t.retryAtMs = ms(e) + (Number(p.delay) || 0) * 1000;
        }
        break;
      }
      case "job.dead": {
        const t = taskOfJob(str(p.job_id));
        setWorker(str(p.worker), null);
        dead.add(str(p.job_id));
        if (t) {
          t.status = "dead";
          t.worker = null;
          t.retryAtMs = null;
        }
        break;
      }
      case "job.replayed": {
        const t = taskOfJob(str(p.job_id));
        dead.delete(str(p.job_id));
        if (t && t.status === "dead") {
          t.status = "queued";
          t.attempt = 0;
        }
        break;
      }
      case "circuit.opened":
        if (str(p.provider) === "llm") circuitOpen = true;
        break;
      case "circuit.closed":
        if (str(p.provider) === "llm") circuitOpen = false;
        break;
    }
  }

  // Zones and slots: stage from the dependency graph, slot = creation order inside the zone.
  const list = [...tasks.values()];
  const stages = stagesOf(list.map((t) => ({ id: t.id, depends_on: deps.get(t.id) ?? [] })));
  const next = new Map<number, number>();
  for (const t of list) {
    t.stage = stages.get(t.id) ?? 1;
    t.zone = LAYOUT.zones.indexOf(zoneForStage(t.stage)) + 1;
    t.slot = next.get(t.zone) ?? 0;
    next.set(t.zone, t.slot + 1);
  }
  const zones = [...next.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([zone, n]) => ({ zone, tasks: n, built: list.filter((t) => t.zone === zone && t.status === "succeeded").length }));

  return {
    goalId, goalStatus, startedMs: started, finishedMs: finished, tasks: list, zones,
    workers: [...workers.values()], circuitOpen, deadJobs: dead.size, retries,
  };
}

/** No goal yet: still show the idle workers (worker.started events) so the plaza is not empty. */
function deriveWorkersOnly(events: TownEvent[]): TownModel {
  const ids = new Set<string>();
  for (const e of events) {
    if (e.type === "worker.started") ids.add(str(e.payload.worker));
    if (e.type === "worker.killed") ids.delete(str(e.payload.worker));
  }
  return { ...EMPTY_MODEL, workers: [...ids].map((id) => ({ id, taskId: null, jobId: null, gone: false })) };
}
