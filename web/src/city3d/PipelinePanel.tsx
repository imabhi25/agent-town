// The pipeline panel and the completion banner for the 3D town. Plain DOM (not WebGL), so it is
// readable by screen readers and testable without a canvas. Everything comes from the TownModel.
import { ZONE_COLORS } from "./palette3d";
import type { BuildingStatus, TownModel } from "./townModel";

const LABEL: Record<BuildingStatus, string> = {
  planned: "planned",
  queued: "queued",
  running: "building",
  retrying: "retrying",
  awaiting_approval: "needs approval",
  succeeded: "built",
  dead: "gave up",
};

export function formatElapsed(ms: number): string {
  const s = Math.max(0, ms) / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

export function PipelinePanel({ model }: { model: TownModel }) {
  if (model.tasks.length === 0) return null;
  const built = model.tasks.filter((t) => t.status === "succeeded").length;
  const done = model.goalStatus === "succeeded";
  const elapsed = model.startedMs !== null && model.finishedMs !== null ? formatElapsed(model.finishedMs - model.startedMs) : null;
  return (
    <section className="panel pipeline" aria-labelledby="pipe-h" data-testid="pipeline">
      <h2 id="pipe-h">Pipeline</h2>
      {done && (
        <p className="banner" role="status">
          Town built{elapsed ? ` in ${elapsed}` : ""}: {built} {built === 1 ? "task" : "tasks"}
          {model.retries > 0 ? `, ${model.retries} ${model.retries === 1 ? "retry" : "retries"}` : ""}
        </p>
      )}
      <ol className="stages">
        {model.zones.map((z) => (
          <li key={z.zone}>
            <span className="stage-head">
              <b className="badge" style={{ background: ZONE_COLORS[z.zone - 1] }}>{z.zone}</b>
              <span className="count3d">{z.built}/{z.tasks} built</span>
            </span>
            <ul>
              {model.tasks.filter((t) => t.zone === z.zone).map((t) => (
                <li key={t.id} className={`task ${t.status}`}>
                  {t.name} <em>{LABEL[t.status]}{t.status === "retrying" ? ` (attempt ${t.attempt})` : ""}</em>
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ol>
    </section>
  );
}
