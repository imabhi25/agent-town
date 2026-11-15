import { useEffect, useRef, useState } from "react";
import type { Api, ApiResult } from "./api";
import { ApprovalCards, ChaosPanel, DeadLetters, Legend } from "./components";
import { CityRenderer } from "./renderer";
import type { DeadJob, PendingApproval, SceneSummary } from "./scene";
import type { StoreState } from "./store";

interface Props {
  state: StoreState;
  api: Api;
  floodMax?: number;
}

interface Overlay {
  summary: SceneSummary;
  deadJobs: DeadJob[];
  approvals: PendingApproval[];
  workerIds: string[];
  failureRate: number | null;
}

const EMPTY: Overlay = {
  summary: { workers: 0, busy: 0, queue: 0, done: 0, dead: 0, circuit: "ok", approvals: 0 },
  deadJobs: [],
  approvals: [],
  workerIds: [],
  failureRate: null,
};

declare global {
  interface Window {
    __town?: { fps: number; summary: () => SceneSummary };
  }
}

export function CityMap({ state, api, floodMax = 100 }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const stateRef = useRef(state);
  stateRef.current = state; // the renderer reads the latest store state each frame
  const apiRef = useRef(api);
  apiRef.current = api;
  const [overlay, setOverlay] = useState<Overlay>(EMPTY);
  const [notice, setNotice] = useState("");

  const notify = (r: ApiResult) => setNotice(r.ok ? "" : r.message);

  useEffect(() => {
    if (!host.current) return;
    const renderer = new CityRenderer(host.current, {
      getState: () => stateRef.current,
      onChange: () => {
        const sc = renderer.scene;
        setOverlay({
          summary: sc.summary(),
          deadJobs: [...sc.deadJobs],
          approvals: [...sc.approvals],
          workerIds: sc.pool.filter((w) => w.active && w.dying === 0).map((w) => w.id).sort(),
          failureRate: sc.failureRate,
        });
      },
      onWorkerTap: (id) => void apiRef.current.killWorker(id).then(notify),
    });
    window.__town = {
      get fps() {
        return renderer.fps;
      },
      summary: () => renderer.scene.summary(),
    };
    void renderer.init();
    return () => {
      renderer.destroy();
      delete window.__town;
    };
  }, []);

  return (
    <div className="town">
      <div className="map-wrap">
        <div className="map" ref={host} data-testid="map" />
        <ApprovalCards approvals={overlay.approvals} api={api} notify={notify} />
      </div>
      <Legend summary={overlay.summary} />
      {notice && (
        <p className="notice" role="alert">
          {notice}
        </p>
      )}
      <div className="panels">
        <ChaosPanel
          api={api}
          workerIds={overlay.workerIds}
          floodMax={floodMax}
          serverRate={overlay.failureRate}
          notify={notify}
        />
        <DeadLetters jobs={overlay.deadJobs} api={api} notify={notify} />
      </div>
    </div>
  );
}
