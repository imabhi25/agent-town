import { Component, lazy, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Api, ApiResult } from "./api";
import { ApprovalCards, ChaosPanel, DeadLetters, Legend } from "./components";
import type { ViewMode } from "./city3d/webgl";
import { CityRenderer } from "./renderer";
import { Scene, type DeadJob, type PendingApproval, type SceneSummary } from "./scene";
import type { StoreState } from "./store";
import { deriveTown } from "./city3d/townModel";
import { PipelinePanel } from "./city3d/PipelinePanel";

// three.js is a separate chunk: it is downloaded only when the 3D view is first shown.
const Scene3D = lazy(() => import("./city3d/Scene3D"));

/** If the 3D chunk fails to load or the scene throws, tell the page so it can show the 2D map. */
class Boundary3D extends Component<{ onFail: (why: string) => void; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(e: Error) {
    this.props.onFail(`The 3D view could not start (${e.message}). Showing the 2D map.`);
  }
  render() {
    return this.state.failed ? null : this.props.children;
  }
}

interface Props {
  state: StoreState;
  view?: ViewMode;
  onViewFail?: (why: string) => void;
  api: Api;
  floodMax?: number;
  off?: string; // why the live controls are disabled ("" = enabled)
  notify?: (r: ApiResult) => void; // lets the page show 429s; default: a local notice line
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

export function CityMap({ state, api, view = "2d", onViewFail, floodMax = 100, off = "", notify: report }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const stateRef = useRef(state);
  stateRef.current = state; // the renderer reads the latest store state each frame
  const apiRef = useRef(api);
  apiRef.current = api;
  const offRef = useRef(off);
  offRef.current = off;
  const model = useMemo(() => deriveTown(state), [state.events]); // eslint-disable-line react-hooks/exhaustive-deps
  const [overlay, setOverlay] = useState<Overlay>(EMPTY);
  const [notice, setNotice] = useState("");

  const notify = (r: ApiResult) => (report ? report(r) : setNotice(r.ok ? "" : r.message));

  // Both views own a Scene; the DOM panels (legend, chaos, DLQ, approvals) read it the same way.
  const showOverlay = (sc: Scene) =>
    setOverlay({
      summary: sc.summary(),
      deadJobs: [...sc.deadJobs],
      approvals: [...sc.approvals],
      workerIds: sc.pool.filter((w) => w.active && w.dying === 0).map((w) => w.id).sort(),
      failureRate: sc.failureRate,
    });
  const onWorkerTap = (id: string) => {
    if (!offRef.current) void apiRef.current.killWorker(id).then(notify); // not on a recording
  };
  // 3D has no PixiJS renderer, so a headless Scene (the same event rules as 2D) feeds the DOM panels:
  // approval cards, dead letters, the worker list, the legend. It polls the store a few times a second.
  useEffect(() => {
    if (view !== "3d") return;
    const scene = new Scene();
    let last = -1;
    const tick = () => {
      scene.sync(stateRef.current.snapshot, stateRef.current.events, stateRef.current.epoch);
      scene.step(1); // let killed workers leave the pool
      if (scene.version !== last) {
        last = scene.version;
        showOverlay(scene);
      }
    };
    tick();
    const id = setInterval(tick, 150);
    window.__town = { fps: 0, summary: () => scene.summary() };
    return () => {
      clearInterval(id);
      delete window.__town;
    };
  }, [view]);
  useEffect(() => {
    if (view !== "2d" || !host.current) return;
    const renderer = new CityRenderer(host.current, {
      getState: () => stateRef.current,
      onChange: () => showOverlay(renderer.scene),
      onWorkerTap,
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
  }, [view]);

  return (
    <div className="town">
      <div className="map-wrap">
        {view === "2d" ? (
          <div className="map" ref={host} data-testid="map" />
        ) : (
          <div className="map map3d" data-testid="map3d">
            <Boundary3D onFail={(why) => onViewFail?.(why)}>
              <Suspense fallback={<p className="muted loading3d">Loading the 3D city…</p>}>
                <Scene3D model={model} onWorkerTap={onWorkerTap} />
              </Suspense>
            </Boundary3D>
          </div>
        )}
        <ApprovalCards approvals={overlay.approvals} api={api} notify={notify} off={off} />
      </div>
      <Legend summary={overlay.summary} />
      {notice && (
        <p className="notice" role="alert">
          {notice}
        </p>
      )}
      {view === "3d" && <PipelinePanel model={model} />}
      <div className="panels">
        <ChaosPanel
          api={api}
          workerIds={overlay.workerIds}
          floodMax={floodMax}
          serverRate={overlay.failureRate}
          notify={notify}
          off={off}
        />
        <DeadLetters jobs={overlay.deadJobs} api={api} notify={notify} off={off} />
      </div>
    </div>
  );
}
