// Small DOM panels around the map. DOM (not canvas) on purpose: real buttons are keyboard and
// screen-reader accessible, and easy to test.
import { useEffect, useState } from "react";
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
}) {
  const { api, workerIds, floodMax, serverRate, notify } = props;
  const [target, setTarget] = useState(""); // "" = a random worker
  const [rate, setRate] = useState(0);
  const [count, setCount] = useState(Math.min(50, floodMax));
  // Follow the server: a change made elsewhere (another tab, curl) moves the slider too.
  useEffect(() => {
    if (typeof serverRate === "number") setRate(serverRate);
  }, [serverRate]);
  return (
    <section className="panel" aria-labelledby="chaos-h">
      <h2 id="chaos-h">Chaos</h2>
      <div className="row">
        <label>
          Worker
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">random</option>
            {workerIds.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </label>
        <button onClick={() => run(() => api.killWorker(target || undefined), notify)}>
          Kill worker
        </button>
        <button onClick={() => run(() => api.startWorker(), notify)}>Start worker</button>
      </div>
      <div className="row">
        <label>
          Failure rate: <output>{Math.round(rate * 100)}%</output>
          <input
            type="range"
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
            min={1}
            max={floodMax}
            value={count}
            onChange={(e) => setCount(Math.max(1, Math.min(floodMax, Number(e.target.value) || 1)))}
          />
        </label>
        <button onClick={() => run(() => api.flood(count), notify)}>Flood {count} mock jobs</button>
      </div>
    </section>
  );
}

export function DeadLetters(props: { jobs: DeadJob[]; api: Api; notify: Notify }) {
  const { jobs, api, notify } = props;
  return (
    <section className="panel" aria-labelledby="dlq-h">
      <h2 id="dlq-h">Dead-letter queue</h2>
      {jobs.length === 0 ? (
        <p className="muted">Nothing dead. Raise the failure rate to see jobs land here.</p>
      ) : (
        <ul className="list">
          {jobs.map((j) => (
            <li key={j.jobId}>
              <span title={j.error || j.type}>
                {j.type ?? "job"} <code>{j.jobId.slice(0, 8)}</code>
              </span>
              <button onClick={() => run(() => api.replay(j.jobId), notify)}>Replay</button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Approval cards sit on top of the map: a person has to decide before the task can run. */
export function ApprovalCards(props: { approvals: PendingApproval[]; api: Api; notify: Notify }) {
  const { approvals, api, notify } = props;
  if (approvals.length === 0) return null;
  return (
    <div className="approvals" role="region" aria-label="Approvals needed">
      {approvals.map((a) => (
        <article className="card" key={a.taskId}>
          <h3>Approval needed</h3>
          <p>{a.name}</p>
          <div className="row">
            <button className="primary" onClick={() => run(() => api.approve(a.taskId), notify)}>
              Approve
            </button>
            <button onClick={() => run(() => api.reject(a.taskId), notify)}>Reject</button>
          </div>
        </article>
      ))}
    </div>
  );
}

export function Legend(props: { summary: SceneSummary }) {
  const s = props.summary;
  return (
    <p className="summary" role="status" aria-live="polite" data-testid="summary">
      {s.workers} workers ({s.busy} busy) · {s.queue} queued · {s.done} done · {s.dead} dead ·
      circuit {s.circuit}
      {s.approvals > 0 ? ` · ${s.approvals} awaiting approval` : ""}
    </p>
  );
}
