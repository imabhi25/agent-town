// Decides what feeds the one event store: a recorded trace or the live backend. Never both.
//
// Rules:
//  - First load: a recorded preset, playing at once (works with no backend at all).
//  - The backend is pinged in the background (health.ts). When it answers, and the visitor has
//    not picked anything yet, the page switches to live by itself.
//  - Every switch resets the store first, so recorded and live events never share a view.
//  - If the live socket drops, fall back to a recording and say why.
import type { ApiResult } from "./api";
import type { Connection } from "./connection";
import type { HealthMonitor, HealthState } from "./health";
import type { Replayer, PlayState, ReplaySink } from "./replay";
import type { TownStore } from "./townStore";
import type { PresetInfo, Trace } from "./types";

export type Mode = "recorded" | "live";

export interface ControllerDeps {
  store: TownStore;
  health: HealthMonitor;
  loadIndex: () => Promise<PresetInfo[]>;
  loadTrace: (preset: string) => Promise<Trace>;
  makeReplayer: (trace: Trace, sink: ReplaySink, onChange: () => void) => Replayer;
  makeConnection: (store: TownStore) => Connection;
  submitGoal: (prompt: string) => Promise<ApiResult>;
}

export interface UiState {
  mode: Mode;
  presets: PresetInfo[];
  preset: string | null;
  loading: boolean;
  error: string; // loading the presets or a trace failed
  notice: string; // a friendly message (429, fell back to a recording, ...)
  play: PlayState;
  speed: number;
  delivered: number;
  total: number;
  health: HealthState;
  healthReason: string;
  /** Empty when live controls work; otherwise the reason they are off (shown as a tooltip). */
  liveOffReason: string;
}

export const TOO_MANY = "The live demo has reached its limit for now. Pick a preset: they always work.";

export class Controller {
  private mode: Mode = "recorded";
  private presets: PresetInfo[] = [];
  private preset: string | null = null;
  private loading = false;
  private error = "";
  private notice = "";
  private replayer: Replayer | null = null;
  private conn: Connection | null = null;
  private token = 0; // bumped on every switch: a slow trace load from before the switch is ignored
  private speed = 1; // kept across presets
  private userChose = false; // after any manual choice, never auto-switch to live
  private listeners = new Set<() => void>();
  private cached: UiState;

  constructor(readonly deps: ControllerDeps) {
    this.cached = this.build();
    deps.health.subscribe(() => this.onHealth());
    deps.store.subscribe(() => this.onStore());
  }

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };
  getUi = () => this.cached;

  /** Load the preset list and start the first one. */
  async init() {
    try {
      this.presets = await this.deps.loadIndex();
    } catch {
      this.error = "Could not load the recorded traces.";
      return this.emit();
    }
    this.emit();
    if (this.mode === "recorded" && this.presets.length) await this.showPreset(this.presets[0].preset);
  }

  /** The visitor picked a recorded preset. */
  async selectPreset(id: string) {
    this.userChose = true;
    this.notice = "";
    await this.showPreset(id);
  }

  async goRecorded() {
    this.userChose = true;
    this.notice = "";
    await this.showPreset(this.preset ?? this.presets[0]?.preset ?? "");
  }

  goLive() {
    if (this.deps.health.state !== "connected") return;
    this.stopSources();
    this.token++;
    this.mode = "live";
    this.notice = "";
    this.error = "";
    this.loading = false;
    this.deps.store.reset(); // nothing recorded may remain in the view
    this.conn = this.deps.makeConnection(this.deps.store);
    this.conn.start();
    this.emit();
  }

  async submitPrompt(prompt: string): Promise<ApiResult | null> {
    if (this.mode !== "live" || !prompt.trim()) return null;
    const r = await this.deps.submitGoal(prompt.trim());
    this.notice = r.ok ? "" : r.status === 429 ? `${r.message} ${TOO_MANY}`.trim() : r.message;
    this.emit();
    return r;
  }

  // ---- playback (recorded mode) ----
  play() { this.replayer?.play(); }
  pause() { this.replayer?.pause(); }
  restart() { this.replayer?.restart(); }
  setSpeed(speed: number) {
    this.speed = speed;
    this.replayer?.setSpeed(speed);
    this.emit();
  }

  /** Report a failed API call (kill, approve, ...) so a 429 gets the same friendly message. */
  report(r: ApiResult) {
    this.notice = r.ok ? "" : r.status === 429 ? `${r.message} ${TOO_MANY}`.trim() : r.message;
    this.emit();
  }

  dispose() {
    this.stopSources();
    this.deps.health.stop();
  }

  // ---- internals ----
  private async showPreset(id: string) {
    if (!id) return;
    this.stopSources();
    const mine = ++this.token;
    this.mode = "recorded";
    this.preset = id;
    this.loading = true;
    this.error = "";
    this.deps.store.reset("replay");
    this.emit();
    let trace: Trace;
    try {
      trace = await this.deps.loadTrace(id);
      if (!Array.isArray(trace?.events)) throw new Error("not a trace");
    } catch {
      if (mine !== this.token) return;
      this.loading = false;
      this.error = "Could not load this recorded trace. Try another preset.";
      return this.emit();
    }
    if (mine !== this.token) return; // the visitor switched while it loaded
    this.loading = false;
    const sink: ReplaySink = {
      reset: () => this.deps.store.reset("replay"),
      push: (msg) => void this.deps.store.dispatch(msg),
    };
    this.replayer = this.deps.makeReplayer(trace, sink, () => this.emit());
    this.replayer.speed = this.speed;
    this.replayer.play();
  }

  private stopSources() {
    this.replayer?.dispose();
    this.replayer = null;
    this.conn?.stop();
    this.conn = null;
  }

  private onHealth() {
    if (this.deps.health.state === "connected" && this.mode === "recorded" && !this.userChose) {
      this.goLive(); // nobody chose anything yet: show the real thing
    }
    this.emit();
  }

  private onStore() {
    // The live socket closed (the backend went away): fall back to a recording.
    if (this.mode === "live" && this.deps.store.status === "closed") {
      this.userChose = true; // do not bounce back to live on our own
      this.notice = "The live backend disconnected, so this is a recorded trace. It will be offered again when the backend is back.";
      this.deps.health.markLost();
      void this.showPreset(this.preset ?? this.presets[0]?.preset ?? "");
    }
  }

  private liveOffReason(): string {
    if (this.mode === "live") return "";
    switch (this.deps.health.state) {
      case "connected": return "You are watching a recorded trace. Switch to Live to use this.";
      case "waking": return "The live backend is waking up. This works once it is connected.";
      case "offline": return this.deps.health.reason || "The live backend is offline. Presets still work.";
    }
  }

  private build(): UiState {
    const r = this.replayer;
    return {
      mode: this.mode,
      presets: this.presets,
      preset: this.preset,
      loading: this.loading,
      error: this.error,
      notice: this.notice,
      play: r?.state ?? "idle",
      speed: this.speed,
      delivered: r?.next ?? 0,
      total: r?.events.length ?? 0,
      health: this.deps.health.state,
      healthReason: this.deps.health.reason,
      liveOffReason: this.liveOffReason(),
    };
  }

  private emit() {
    this.cached = this.build();
    this.listeners.forEach((fn) => fn());
  }
}
