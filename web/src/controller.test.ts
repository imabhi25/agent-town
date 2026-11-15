import { describe, expect, it } from "vitest";
import { Connection, type SocketLike } from "./connection";
import { Controller, TOO_MANY } from "./controller";
import { HealthMonitor } from "./health";
import { Replayer } from "./replay";
import { TownStore } from "./townStore";
import type { Snapshot, TownEvent, Trace } from "./types";

const ev = (id: number, ms: number, type = "job.started"): TownEvent => ({
  id, stream_id: `${id}-0`, type, payload: {}, ts: new Date(Date.UTC(2026, 0, 1) + ms).toISOString(),
});
const snap = (events: TownEvent[] = []): Snapshot => ({ type: "snapshot", goals: [], tasks: [], workers: [], queue: {}, stats: {}, events, last_event_id: "0-0" });
const mkTrace = (preset: string, base: number): Trace => ({
  preset, name: preset, description: `about ${preset}`, recorded_at: "", snapshot: snap(),
  events: [ev(base + 1, 0), ev(base + 2, 1000)],
});

class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  close() { this.closed = true; this.onclose?.(); }
  send(msg: unknown) { this.onmessage?.({ data: JSON.stringify(msg) }); }
}

function setup(opts: { traces?: Record<string, Trace>; submit?: (p: string) => { ok: boolean; status: number; message: string } } = {}) {
  let t = 0, id = 1;
  const timers = new Map<number, { at: number; fn: () => void }>();
  const clk = {
    now: () => t,
    setTimer: (fn: () => void, ms: number) => (timers.set(id, { at: t + ms, fn }), id++),
    clearTimer: (x: unknown) => void timers.delete(x as number),
  };
  const tick = (ms: number) => {
    const end = t + ms;
    for (;;) {
      const due = [...timers.entries()].filter(([, x]) => x.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]); t = due[1].at; due[1].fn();
    }
    t = end;
  };
  const traces = opts.traces ?? { a: mkTrace("a", 0), b: mkTrace("b", 100) };
  const index = Object.values(traces).map((x) => ({ preset: x.preset, name: x.name, description: x.description }));
  let backendUp = false;
  const sockets: FakeSocket[] = [];
  const store = new TownStore();
  const health = new HealthMonitor({
    ping: async () => ({ ok: backendUp }),
    now: clk.now,
    // health uses its own timers so tick() for the replay does not also ping
    setTimer: () => 0,
    clearTimer: () => {},
  });
  const loads: { preset: string; resolve: () => void }[] = [];
  let holdLoads = false;
  const ctl = new Controller({
    store, health,
    loadIndex: async () => index,
    loadTrace: (p) => holdLoads
      ? new Promise((res) => loads.push({ preset: p, resolve: () => res(traces[p]) }))
      : Promise.resolve(traces[p]),
    makeReplayer: (trace, sink, onChange) => new Replayer(trace, sink, clk, onChange),
    makeConnection: (s) => new Connection({
      apiUrl: "http://api.test",
      openSocket: () => { const sk = new FakeSocket(); sockets.push(sk); return sk; },
      fetchJson: async () => snap(),
      setTimer: () => 0, clearTimer: () => {},
    }, s),
    submitGoal: async (p) => opts.submit?.(p) ?? { ok: true, status: 201, message: "" },
  });
  const flush = () => new Promise((r) => setTimeout(r, 0));
  const ids = () => store.state.events.map((e) => e.id);
  return {
    ctl, store, health, sockets, tick, flush, ids, loads,
    hold: () => { holdLoads = true; },
    async backendComesUp() { backendUp = true; health.start(); await flush(); },
  };
}

describe("Controller", () => {
  it("starts on the first recorded preset, playing, with no backend", async () => {
    const s = setup();
    await s.ctl.init();
    s.tick(0);
    const ui = s.ctl.getUi();
    expect(ui.mode).toBe("recorded");
    expect(ui.preset).toBe("a");
    expect(ui.presets.map((p) => p.preset)).toEqual(["a", "b"]);
    expect(s.ids()).toEqual([1]);
    s.tick(1000);
    expect(s.ids()).toEqual([1, 2]);
    expect(s.store.status).toBe("replay");
  });

  it("switches to live by itself when the backend wakes and nobody chose anything", async () => {
    const s = setup();
    await s.ctl.init();
    s.tick(0);
    expect(s.ids()).toEqual([1]);
    await s.backendComesUp();
    expect(s.ctl.getUi().mode).toBe("live");
    expect(s.sockets).toHaveLength(1);
    expect(s.ids()).toEqual([]); // the recorded event is gone
    s.sockets[0].onopen?.();
    s.sockets[0].send({ type: "events", events: [ev(900, 0)] });
    expect(s.ids()).toEqual([900]);
    // the replay is stopped: it must not push anything more into the live store
    s.tick(10_000);
    expect(s.ids()).toEqual([900]);
  });

  it("does not auto-switch after the visitor picked a preset", async () => {
    const s = setup();
    await s.ctl.init();
    await s.ctl.selectPreset("b");
    await s.backendComesUp();
    expect(s.ctl.getUi().mode).toBe("recorded");
    expect(s.ctl.getUi().preset).toBe("b");
    expect(s.sockets).toHaveLength(0);
  });

  it("goLive is refused while the backend is not connected", async () => {
    const s = setup();
    await s.ctl.init();
    s.ctl.goLive();
    expect(s.ctl.getUi().mode).toBe("recorded");
    expect(s.sockets).toHaveLength(0);
  });

  it("recorded -> live -> recorded: each switch clears the store, nothing mixes", async () => {
    const s = setup();
    await s.ctl.init();
    await s.backendComesUp(); // goes live (nothing chosen yet)
    s.sockets[0].onopen?.();
    s.sockets[0].send({ type: "events", events: [ev(900, 0)] });
    expect(s.ids()).toEqual([900]);

    await s.ctl.goRecorded();
    expect(s.sockets[0].closed).toBe(true);
    expect(s.ids()).toEqual([]);
    s.tick(0);
    expect(s.ids()).toEqual([1]); // only recorded ids, no 900
    s.sockets[0].send({ type: "events", events: [ev(901, 0)] }); // a late message from the old socket
    expect(s.ids()).toEqual([1]);

    s.ctl.goLive();
    expect(s.ids()).toEqual([]); // the recorded event is gone again
    expect(s.sockets).toHaveLength(2);
  });

  it("ignores a trace that finishes loading after the visitor switched away", async () => {
    const s = setup();
    await s.ctl.init();
    s.hold();
    const slow = s.ctl.selectPreset("a");
    const fast = s.ctl.selectPreset("b");
    s.loads.find((l) => l.preset === "b")!.resolve();
    await fast;
    s.loads.find((l) => l.preset === "a")!.resolve(); // the stale one arrives last
    await slow;
    s.tick(0);
    expect(s.ctl.getUi().preset).toBe("b");
    expect(s.ids()).toEqual([101]); // only b's events
  });

  it("shows an error, not a blank map, when a trace cannot be loaded", async () => {
    const s = setup({ traces: { a: mkTrace("a", 0) } });
    await s.ctl.init();
    await s.ctl.selectPreset("missing");
    expect(s.ctl.getUi().error).toMatch(/Could not load/);
    expect(s.ctl.getUi().loading).toBe(false);
  });

  it("shows an error when the preset list cannot be loaded", async () => {
    const s = setup();
    (s.ctl as unknown as { deps: { loadIndex: () => Promise<never> } }).deps.loadIndex = () => Promise.reject(new Error("x"));
    await s.ctl.init();
    expect(s.ctl.getUi().error).toMatch(/Could not load/);
  });

  it("falls back to a recording, with a message, when the live socket drops", async () => {
    const s = setup();
    await s.ctl.init();
    await s.backendComesUp();
    s.sockets[0].onopen?.();
    s.sockets[0].close(); // the backend went away
    await s.flush();
    const ui = s.ctl.getUi();
    expect(ui.mode).toBe("recorded");
    expect(ui.notice).toMatch(/disconnected/);
    expect(ui.health).toBe("offline");
    s.tick(0);
    expect(s.ids()).toEqual([1]);
  });

  it("explains why live controls are off, depending on the state", async () => {
    const s = setup();
    await s.ctl.init();
    expect(s.ctl.getUi().liveOffReason).toMatch(/waking/);
    s.health.state = "offline";
    s.ctl.setSpeed(1); // any emit rebuilds the ui state
    expect(s.ctl.getUi().liveOffReason).toMatch(/offline/);
    s.health.state = "connected";
    s.ctl.setSpeed(1);
    expect(s.ctl.getUi().liveOffReason).toMatch(/Switch to Live/);
  });

  it("live controls are enabled in live mode", async () => {
    const s = setup();
    await s.ctl.init();
    await s.backendComesUp();
    expect(s.ctl.getUi().liveOffReason).toBe("");
  });

  it("a prompt is ignored in recorded mode and sent in live mode", async () => {
    const sent: string[] = [];
    const s = setup({ submit: (p) => (sent.push(p), { ok: true, status: 201, message: "" }) });
    await s.ctl.init();
    expect(await s.ctl.submitPrompt("hello")).toBeNull();
    await s.backendComesUp();
    await s.ctl.submitPrompt("  plan a trip ");
    expect(sent).toEqual(["plan a trip"]);
  });

  it("turns a 429 into a friendly message", async () => {
    const s = setup({ submit: () => ({ ok: false, status: 429, message: "Daily live-goal cap reached." }) });
    await s.ctl.init();
    await s.backendComesUp();
    await s.ctl.submitPrompt("x");
    expect(s.ctl.getUi().notice).toBe(`Daily live-goal cap reached. ${TOO_MANY}`);
    expect(s.ctl.getUi().mode).toBe("live"); // stays live; presets are one click away
  });

  it("keeps the chosen speed across presets", async () => {
    const s = setup();
    await s.ctl.init();
    s.ctl.setSpeed(4);
    await s.ctl.selectPreset("b");
    expect(s.ctl.getUi().speed).toBe(4);
    s.tick(250);
    expect(s.ids()).toEqual([101, 102]);
  });
});
