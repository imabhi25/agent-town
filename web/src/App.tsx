import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { createApi, parseConfig, type PublicConfig } from "./api";
import { CityMap } from "./CityMap";
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
  const view = useSyncExternalStore(ctl.deps.store.subscribe, ctl.deps.store.getView);
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
      <header>
        <h1>Agent Town</h1>
        <StatusPill ui={ui} />
        <ModeBar ui={ui} a={actions} />
        <span className="muted" data-testid="mode">
          {ui.mode === "live"
            ? view.status === "open"
              ? "Showing: live"
              : "Showing: live (connecting…)"
            : "Showing: recorded trace"}
        </span>
      </header>
      {ui.notice && (
        <p className="notice" role="alert" data-testid="notice">
          {ui.notice}
        </p>
      )}
      <div className="panels top">
        <PresetPicker ui={ui} a={actions} />
        {config?.planner === "gemini" && <PromptBox ui={ui} a={actions} />}
      </div>
      <CityMap state={view} api={api} off={ui.liveOffReason} notify={(r) => ctl.report(r)} />
    </main>
  );
}
