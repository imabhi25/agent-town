import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { createApi, parseConfig, type PublicConfig } from "./api";
import { CityMap } from "./CityMap";
import { defaultView, parseViewOverride, prefersReducedMotion, supportsWebGL2, type ViewMode } from "./city3d/webgl";
import { Connection } from "./connection";
import { Controller } from "./controller";
import { ModeBar, PresetPicker, PromptBox, StatusPill } from "./controls";
import { HealthMonitor } from "./health";
import { Replayer } from "./replay";
import { TownStore } from "./townStore";
import type { PresetInfo, Trace } from "./types";
import "./styles.css";

const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";
const timers = {
  setTimer: (fn: () => void, ms: number) => setTimeout(fn, ms),
  clearTimer: (t: unknown) => clearTimeout(t as number),
};

async function getJson<T>(url: string, timeoutMs?: number): Promise<T> {
  const r = await fetch(url, timeoutMs ? { signal: AbortSignal.timeout(timeoutMs) } : undefined);
  if (!r.ok) throw new Error(String(r.status));
  return r.json() as Promise<T>;
}

export function App() {
  const api = useMemo(() => createApi(API_URL), []);
  const ctl = useMemo(() => {
    const store = new TownStore();
    const health = new HealthMonitor({
      ping: async () => {
        // A sleeping free host can hang for a long time: give up on one ping after 10s.
        const body = await getJson<{ status?: string; live?: boolean }>(`${API_URL}/healthz`, 10_000);
        return { ok: body.status === "ok", live: body.live };
      },
      now: () => Date.now(),
      ...timers,
    });
    return new Controller({
      store,
      health,
      loadIndex: () => getJson<PresetInfo[]>("/traces/index.json"),
      loadTrace: (id) => getJson<Trace>(`/traces/${encodeURIComponent(id)}.json`),
      makeReplayer: (trace, sink, onChange) =>
        new Replayer(trace, sink, { now: () => performance.now(), ...timers }, onChange),
      makeConnection: (s) =>
        new Connection(
          {
            apiUrl: API_URL,
            openSocket: (url) => new WebSocket(url) as never,
            fetchJson: (url) => getJson(url),
            ...timers,
          },
          s,
        ),
      submitGoal: (prompt) => api.submitGoal(prompt),
    });
  }, [api]);

  useEffect(() => {
    ctl.deps.health.start();
    void ctl.init();
    return () => ctl.dispose();
  }, [ctl]);

  // Which planner the backend runs. Unknown until it answers; the free-text box needs "gemini".
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const webgl2 = useMemo(() => supportsWebGL2(), []);
  const [view, setView] = useState<ViewMode>(() =>
    defaultView({ webgl2, reducedMotion: prefersReducedMotion(), override: parseViewOverride(location.search) }),
  );
  const [viewNotice, setViewNotice] = useState("");
  const ui = useSyncExternalStore(ctl.subscribe, ctl.getUi);
  useEffect(() => {
    if (ui.health !== "connected" || config) return;
    let live = true;
    getJson<unknown>(`${API_URL}/config`, 10_000)
      .then((b) => live && setConfig(parseConfig(b)))
      .catch(() => undefined); // stays hidden; asked again if the connection state changes
    return () => {
      live = false;
    };
  }, [ui.health, config]);
  const store = useSyncExternalStore(ctl.deps.store.subscribe, ctl.deps.store.getView);
  const actions = {
    selectPreset: (id: string) => void ctl.selectPreset(id),
    goLive: () => ctl.goLive(),
    goRecorded: () => void ctl.goRecorded(),
    play: () => ctl.play(),
    pause: () => ctl.pause(),
    restart: () => ctl.restart(),
    setSpeed: (s: number) => ctl.setSpeed(s),
    submitPrompt: (t: string) => ctl.submitPrompt(t),
  };

  return (
    <main>
      <header className="hero">
        <div>
          <h1>Agent Town</h1>
          <p className="tagline">
            A live map of a real job engine. A goal is planned into a task graph, run by workers on Redis
            Streams with retries, a dead-letter queue and human approvals, and streamed here as events.
          </p>
        </div>
        <a className="gh" href="https://github.com/imabhi25/agent-town" target="_blank" rel="noreferrer">
          View on GitHub
        </a>
      </header>
      <ol className="flow" aria-label="How a goal moves through the engine">
        {["Prompt", "Gemini planner", "Task DAG", "Redis Streams", "Workers", "WebSocket", "This map"].map((s) => (
          <li key={s}>{s}</li>
        ))}
      </ol>
      <div className="toolbar">
        <StatusPill ui={ui} />
        <ModeBar ui={ui} a={actions} />
        <div className="modebar" role="group" aria-label="View">
          <button aria-pressed={view === "3d"} disabled={!webgl2} title={webgl2 ? undefined : "This device has no WebGL2, so only the 2D map is available."} onClick={() => setView("3d")}>
            3D
          </button>
          <button aria-pressed={view === "2d"} onClick={() => setView("2d")}>
            2D
          </button>
        </div>
        <span className="muted" data-testid="mode">
          {ui.mode === "live"
            ? store.status === "open"
              ? "Showing: live"
              : "Showing: live (connecting…)"
            : "Showing: recorded trace"}
        </span>
      </div>
      {ui.notice && (
        <p className="notice" role="alert" data-testid="notice">
          {ui.notice}
        </p>
      )}
      {viewNotice && (
        <p className="notice" role="status" data-testid="view-notice">
          {viewNotice}
        </p>
      )}
      <CityMap
        view={view}
        onViewFail={(why) => {
          setViewNotice(why);
          setView("2d");
        }}
        state={store}
        api={api}
        off={ui.liveOffReason}
        notify={(r) => ctl.report(r)}
        sidebar={
          <>
            <PresetPicker ui={ui} a={actions} />
            {config?.planner === "gemini" && <PromptBox ui={ui} a={actions} />}
          </>
        }
      />
    </main>
  );
}
