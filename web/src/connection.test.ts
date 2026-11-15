import { describe, expect, it } from "vitest";
import { Connection, wsUrl, type SocketLike } from "./connection";
import type { Snapshot, TownEvent } from "./types";

class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {}
  close() { this.closed = true; this.onclose?.(); }
  send(msg: unknown) { this.onmessage?.({ data: JSON.stringify(msg) }); }
}

const ev = (id: number): TownEvent => ({ id, stream_id: `${id}-0`, type: "t", payload: {}, ts: `2026-01-01T00:00:0${id}.000+00:00` });
const snap = (events: TownEvent[]): Snapshot => ({ type: "snapshot", goals: [], tasks: [], workers: [], queue: {}, stats: {}, events, last_event_id: "0-0" });

function setup(fetchImpl: (url: string) => Promise<unknown> = async () => snap([ev(1), ev(2), ev(3)])) {
  const sockets: FakeSocket[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const fetched: string[] = [];
  const conn = new Connection({
    apiUrl: "http://api.test",
    openSocket: (url) => { const s = new FakeSocket(url); sockets.push(s); return s; },
    fetchJson: (url) => { fetched.push(url); return fetchImpl(url); },
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimer: () => {},
  });
  return { conn, sockets, timers, fetched };
}

describe("Connection", () => {
  it("derives the ws url", () => {
    expect(wsUrl("http://x:8000")).toBe("ws://x:8000/ws");
    expect(wsUrl("https://x.onrender.com")).toBe("wss://x.onrender.com/ws");
  });

  it("fetches /state once when a batch reports dropped events", async () => {
    const { conn, sockets, fetched } = setup();
    conn.start();
    sockets[0].onopen?.();
    sockets[0].send({ type: "events", events: [ev(1)], dropped: 5 });
    sockets[0].send({ type: "events", events: [ev(2)], dropped: 2 }); // while the first is in flight
    await new Promise((r) => setTimeout(r, 0));
    expect(fetched).toEqual(["http://api.test/state"]);
    expect(conn.view.events.map((e) => e.id)).toEqual([1, 2, 3]);
    expect(conn.view.snapshot).not.toBeNull();
  });

  it("can fetch again after the first refresh finished", async () => {
    const { conn, sockets, fetched } = setup();
    conn.start();
    sockets[0].send({ type: "events", events: [ev(1)], dropped: 1 });
    await new Promise((r) => setTimeout(r, 0));
    sockets[0].send({ type: "events", events: [ev(2)], dropped: 1 });
    await new Promise((r) => setTimeout(r, 0));
    expect(fetched).toHaveLength(2);
  });

  it("survives a failed /state fetch", async () => {
    const { conn, sockets } = setup(async () => { throw new Error("down"); });
    conn.start();
    sockets[0].send({ type: "events", events: [ev(1)], dropped: 1 });
    await new Promise((r) => setTimeout(r, 0));
    expect(conn.view.events.map((e) => e.id)).toEqual([1]);
  });

  it("reconnects with backoff and merges the new snapshot without duplicates", () => {
    const { conn, sockets, timers } = setup();
    conn.start();
    sockets[0].onopen?.();
    sockets[0].send(snap([ev(1), ev(2)]));
    sockets[0].close(); // connection drops
    expect(conn.view.status).toBe("closed");
    expect(timers[0].ms).toBe(1000);
    timers[0].fn(); // reconnect timer fires
    expect(sockets).toHaveLength(2);
    sockets[1].send(snap([ev(2), ev(3)])); // server pushes a snapshot on connect
    sockets[1].onopen?.();
    expect(conn.view.status).toBe("open");
    expect(conn.view.events.map((e) => e.id)).toEqual([1, 2, 3]);
  });

  it("backs off 1s, 2s, 4s while the server stays down, then resets after a successful open", () => {
    const { conn, sockets, timers } = setup();
    conn.start();
    for (let i = 0; i < 3; i++) { sockets[i].close(); timers[i].fn(); }
    expect(timers.map((t) => t.ms)).toEqual([1000, 2000, 4000]);
    sockets[3].onopen?.();
    sockets[3].close();
    expect(timers[3].ms).toBe(1000);
  });

  it("does not reconnect after stop()", () => {
    const { conn, sockets, timers } = setup();
    conn.start();
    conn.stop();
    expect(sockets[0].closed).toBe(true);
    expect(timers).toHaveLength(0);
  });
});
