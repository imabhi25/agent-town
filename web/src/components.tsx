// Small DOM panels around the map. DOM (not canvas) on purpose: real buttons are keyboard and
// screen-reader accessible, and easy to test.
import { useEffect, useState } from "react";
import { Icon } from "./Icon";
import type { Api, ApiResult } from "./api";
import type { DeadJob, PendingApproval, SceneSummary } from "./scene";

type Notify = (r: ApiResult) => void;

/** Run an API call and report a failure to the notice line. */
async function run(call: () => Promise<ApiResult>, notify: Notify) {
  notify(await call());
}

export function ChaosPanel(props: {
  api: Api;
  workerIds: string[];
  floodMax: number;
  serverRate?: number | null; // the rate the server last reported (from chaos.config events)
  notify: Notify;
  off?: string; // why the controls are disabled (a recording is playing); "" = enabled
}) {
  const { api, workerIds, floodMax, serverRate, notify } = props;
  const off = props.off ?? "";
  const [target, setTarget] = useState(""); // "" = a random worker
  const targetGone = target !== "" && !workerIds.includes(target);
  const [rate, setRate] = useState(0);
  const [count, setCount] = useState(Math.min(50, floodMax));
  // Follow the server: a change made elsewhere (another tab, curl) moves the slider too.
  useEffect(() => {
    if (typeof serverRate === "number") setRate(serverRate);
  }, [serverRate]);
  return (
    <section className="live-control" aria-labelledby="chaos-h" title={off || undefined}>
      <div className="control-head">
        <span className="control-icon"><Icon name="bolt" /></span>
        <div className="control-label">
          <h3 id="chaos-h">Chaos</h3>
          <p className="muted">Simulate a worker failure</p>
        </div>
        <button className="danger" disabled={!!off || targetGone} onClick={() => run(() => api.killWorker(target || undefined), notify)}>
          Kill worker
        </button>
      </div>
      <details className="chaos-settings">
        <summary>Chaos settings</summary>
        <div className="row">
          <label>
            Worker
            <select disabled={!!off} value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">random</option>
              {targetGone && <option value={target} disabled>{target} (offline)</option>}
              {workerIds.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
          </label>
          <button disabled={!!off} onClick={() => run(() => api.startWorker(), notify)}>Start worker</button>
        </div>
        <div className="row">
          <label>
            Failure rate: <output>{Math.round(rate * 100)}%</output>
            <input
              type="range"
              disabled={!!off}
              min={0}
              max={1}
              step={0.05}
              value={rate}
              onChange={(e) => setRate(Number(e.target.value))}
              onPointerUp={() => run(() => api.setFailureRate(rate), notify)}
              onKeyUp={() => run(() => api.setFailureRate(rate), notify)}
            />
          </label>
        </div>
        <div className="row">
          <label>
            Flood
            <input
              type="number"
              disabled={!!off}
              min={1}
              max={floodMax}
              value={count}
              onChange={(e) => setCount(Math.max(1, Math.min(floodMax, Number(e.target.value) || 1)))}
            />
          </label>
          <button disabled={!!off} onClick={() => run(() => api.flood(count), notify)}>Flood {count} mock jobs</button>
        </div>
      </details>
    </section>
  );
}

export function DeadLetters(props: { jobs: DeadJob[]; api: Api; notify: Notify; off?: string }) {
  const { jobs, api, notify } = props;
  const off = props.off ?? "";
  return (
    <section className="live-control" aria-labelledby="dlq-h">
      <div className="control-head">
        <span className="control-icon"><Icon name="tray" /></span>
        <div className="control-label">
          <h3 id="dlq-h">Dead-letter queue</h3>
          <p className="muted">{jobs.length === 0 ? "No failed jobs" : "Tasks that exhausted retries"}</p>
        </div>
        <span className={jobs.length ? "queue-count has-jobs" : "queue-count"} aria-label={`${jobs.length} dead-letter jobs`}>{jobs.length}</span>
      </div>
      {jobs.length > 0 && (
        <ul className="list">
          {jobs.map((j) => (
            <li key={j.jobId}>
              <span title={j.error || j.type}>
                {j.type ?? "job"} <code>{j.jobId.slice(0, 8)}</code>
              </span>
              <button disabled={!!off} title={off || undefined} onClick={() => run(() => api.replay(j.jobId), notify)}>
                Replay
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Approval cards sit on top of the map: a person has to decide before the task can run. */
export function ApprovalCards(props: {
  approvals: PendingApproval[];
  api: Api;
  notify: Notify;
  off?: string;
}) {
  const { approvals, api, notify } = props;
  const off = props.off ?? "";
  if (approvals.length === 0) return null;
  return (
    <div className="approvals" role="region" aria-label="Approvals needed">
      {approvals.map((a) => (
        <article className="card" key={a.taskId}>
          <h3>Approval needed</h3>
          <p>{a.name}</p>
          <div className="row">
            <button className="primary" disabled={!!off} title={off || undefined} onClick={() => run(() => api.approve(a.taskId), notify)}>
              Approve
            </button>
            <button disabled={!!off} title={off || undefined} onClick={() => run(() => api.reject(a.taskId), notify)}>
              Reject
            </button>
          </div>
        </article>
      ))}
    </div>
  );
}

export function Legend(props: { summary: SceneSummary }) {
  const s = props.summary;
  const counters = [
    { key: "workers", count: s.workers },
    { key: "busy", count: s.busy },
    { key: "queued", count: s.queue },
    { key: "done", count: s.done },
    { key: "failed", count: s.dead },
  ];
  return (
    <p className="summary stats" role="status" aria-live="polite" data-testid="summary">
      {counters.map(({ key, count }, i) => (
        <span className={`stat ${key}`} key={key}>
          {i > 0 && <span className="sr-only"> · </span>}
          <b>{count}</b> <span className="stat-name">{key}</span>
        </span>
      ))}
      <span className={s.circuit === "tripped" ? "stat failed" : "sr-only"}><span className="sr-only"> · </span>circuit {s.circuit}</span>
      {s.approvals > 0 && (
        <span className="stat ask"><span className="sr-only"> · </span><b>{s.approvals}</b> awaiting approval</span>
      )}
    </p>
  );
}
