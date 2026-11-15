// Is the live backend awake? A free host sleeps when idle and takes up to ~a minute to wake, so
// the page starts on a recorded trace and pings GET /healthz in the background.
//
//   waking    pinging every 5s for up to 60s ("Recorded Trace (Waking Live Backend...)")
//   connected the backend answered ("Live Backend Connected")
//   offline   60s passed without an answer; keep trying every 15s in the background
//
// Everything outside (the request, the clock) is injected, so the state machine is unit-tested.
export type HealthState = "waking" | "connected" | "offline";

export interface HealthResult {
  ok: boolean; // the backend answered
  live?: boolean; // /healthz `live`: false when the free-tier budget has closed live mode
}

export interface HealthDeps {
  ping: () => Promise<HealthResult>;
  setTimer: (fn: () => void, ms: number) => unknown;
  clearTimer: (t: unknown) => void;
  now: () => number;
}

export const WAKE_POLL_MS = 5_000;
export const WAKE_TIMEOUT_MS = 60_000;
export const OFFLINE_POLL_MS = 15_000;

export class HealthMonitor {
  state: HealthState = "waking";
  /** Why it is not connected, when the backend answered but live mode is closed. */
  reason = "";
  private startedAt = 0;
  private timer: unknown = null;
  private running = false;
  private listeners = new Set<() => void>();

  constructor(private deps: HealthDeps) {}

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  start() {
    this.running = true;
    this.startedAt = this.deps.now();
    this.set("waking");
    void this.check();
  }

  stop() {
    this.running = false;
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.timer = null;
  }

  /** The live connection dropped: go back to looking for the backend. */
  markLost() {
    if (!this.running || this.state !== "connected") return;
    this.set("offline");
    this.schedule(OFFLINE_POLL_MS);
  }

  private set(state: HealthState, reason = "") {
    if (state === this.state && reason === this.reason) return;
    this.state = state;
    this.reason = reason;
    this.listeners.forEach((fn) => fn());
  }

  private schedule(ms: number) {
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.timer = this.deps.setTimer(() => void this.check(), ms);
  }

  private async check() {
    this.timer = null;
    let result: HealthResult;
    try {
      result = await this.deps.ping();
    } catch {
      result = { ok: false };
    }
    if (!this.running) return;
    if (result.ok && result.live !== false) {
      this.set("connected");
      return; // no more polling: the live socket reports its own trouble (markLost)
    }
    if (result.ok) {
      // Awake, but live mode is closed (the free Redis budget). Not worth waking up for: offline.
      this.set("offline", "Live mode is paused to stay inside the free-tier limits.");
      return this.schedule(OFFLINE_POLL_MS);
    }
    const waited = this.deps.now() - this.startedAt;
    if (this.state === "waking" && waited < WAKE_TIMEOUT_MS) return this.schedule(WAKE_POLL_MS);
    this.set("offline");
    this.schedule(OFFLINE_POLL_MS);
  }
}

export const PILL: Record<HealthState, string> = {
  waking: "Recorded Trace (Waking Live Backend...)",
  connected: "Live Backend Connected",
  offline: "Recorded Trace (Live Backend Offline)",
};
