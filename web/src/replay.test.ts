import { describe, expect, it } from "vitest";
import { prepareEvents, Replayer } from "./replay";
import type { ServerMessage, Snapshot, TownEvent, Trace } from "./types";

// A fake clock: timers fire only when the test advances time, so timing is exact.
function clock() {
  let t = 0;
  let nextId = 1;
  const timers = new Map<number, { at: number; fn: () => void }>();
  return {
    now: () => t,
    setTimer: (fn: () => void, ms: number) => {
      timers.set(nextId, { at: t + ms, fn });
      return nextId++;
    },
    clearTimer: (id: unknown) => void timers.delete(id as number),
    pending: () => timers.size,
    advance(ms: number) {
      const end = t + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, x]) => x.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]);
        t = due[1].at;
        due[1].fn();
      }
      t = end;
    },
  };
}

const ev = (id: number, ms: number, type = "x"): TownEvent => ({
  id,
  stream_id: `${id}-0`,
  type,
  payload: {},
  ts: new Date(Date.UTC(2026, 0, 1) + ms).toISOString(),
});
const snap: Snapshot = { type: "snapshot", goals: [], tasks: [], workers: [], queue: {}, stats: {}, events: [], last_event_id: "0-0" };
const trace = (events: TownEvent[]): Trace => ({ preset: "p", name: "P", description: "", recorded_at: "", snapshot: snap, events });

function setup(events: TownEvent[]) {
  const c = clock();
  const got: ServerMessage[] = [];
  let resets = 0;
  const r = new Replayer(trace(events), { reset: () => resets++, push: (m) => got.push(m) }, c);
  const ids = () => got.flatMap((m) => (m.type === "events" ? m.events.map((e) => e.id) : []));
  return { c, r, got, ids, resets: () => resets };
}

describe("prepareEvents", () => {
  it("drops duplicate ids and sorts by (ts, id)", () => {
    const out = prepareEvents([ev(3, 2000), ev(1, 0), ev(2, 0), ev(3, 2000)]);
    expect(out.map((e) => e.id)).toEqual([1, 2, 3]);
  });
  it("breaks timestamp ties by id", () => {
    expect(prepareEvents([ev(9, 500), ev(4, 500)]).map((e) => e.id)).toEqual([4, 9]);
  });
});

describe("Replayer timing", () => {
  it("sends the snapshot first, then each event after its recorded gap", () => {
    const { c, r, got, ids } = setup([ev(1, 0), ev(2, 1000), ev(3, 3000)]);
    r.play();
    expect(got[0].type).toBe("snapshot");
    c.advance(0);
    expect(ids()).toEqual([1]);
    c.advance(999);
    expect(ids()).toEqual([1]);
    c.advance(1);
    expect(ids()).toEqual([1, 2]);
    c.advance(1999);
    expect(ids()).toEqual([1, 2]);
    c.advance(1);
    expect(ids()).toEqual([1, 2, 3]);
    expect(r.state).toBe("ended");
  });

  it("delivers events that are due together as one batch", () => {
    const { c, r, got } = setup([ev(1, 0), ev(2, 0), ev(3, 0), ev(4, 500)]);
    r.play();
    c.advance(0);
    const batches = got.filter((m) => m.type === "events");
    expect(batches).toHaveLength(1);
    expect(batches[0].type === "events" && batches[0].events.map((e) => e.id)).toEqual([1, 2, 3]);
  });

  it("delivers in (ts, id) order even if the trace file is shuffled", () => {
    const { c, r, ids } = setup([ev(3, 2000), ev(1, 0), ev(2, 1000)]);
    r.play();
    c.advance(5000);
    expect(ids()).toEqual([1, 2, 3]);
  });

  it("delivers a duplicate id only once", () => {
    const { c, r, ids } = setup([ev(1, 0), ev(1, 0), ev(2, 100)]);
    r.play();
    c.advance(1000);
    expect(ids()).toEqual([1, 2]);
  });
});

describe("Replayer pause, resume, speed, restart", () => {
  it("pauses: nothing arrives while paused, and the remaining gap is kept on resume", () => {
    const { c, r, ids } = setup([ev(1, 0), ev(2, 1000)]);
    r.play();
    c.advance(400);
    r.pause();
    expect(c.pending()).toBe(0);
    c.advance(10_000);
    expect(ids()).toEqual([1]);
    expect(r.position).toBe(400);
    r.play();
    c.advance(599);
    expect(ids()).toEqual([1]);
    c.advance(1);
    expect(ids()).toEqual([1, 2]);
  });

  it("2x and 4x shorten the gaps", () => {
    for (const [speed, wait] of [[2, 500], [4, 250]] as const) {
      const { c, r, ids } = setup([ev(1, 0), ev(2, 1000)]);
      r.setSpeed(speed);
      r.play();
      c.advance(wait - 1);
      expect(ids()).toEqual([1]);
      c.advance(1);
      expect(ids()).toEqual([1, 2]);
    }
  });

  it("changing speed mid-play keeps the position and reschedules", () => {
    const { c, r, ids } = setup([ev(1, 0), ev(2, 2000)]);
    r.play();
    c.advance(1000); // halfway to event 2
    r.setSpeed(4); // the other 1000ms now take 250ms
    c.advance(249);
    expect(ids()).toEqual([1]);
    c.advance(1);
    expect(ids()).toEqual([1, 2]);
  });

  it("restart resets the store and plays from the beginning", () => {
    const { c, r, got, ids, resets } = setup([ev(1, 0), ev(2, 500)]);
    r.play();
    c.advance(500);
    expect(r.state).toBe("ended");
    r.restart();
    expect(resets()).toBe(1);
    expect(got.filter((m) => m.type === "snapshot")).toHaveLength(2);
    c.advance(500);
    expect(ids()).toEqual([1, 2, 1, 2]); // the store dedupes across a reset because it was cleared
    expect(r.state).toBe("ended");
  });

  it("play after the end starts over", () => {
    const { c, r, resets } = setup([ev(1, 0)]);
    r.play();
    c.advance(0);
    expect(r.state).toBe("ended");
    r.play();
    expect(resets()).toBe(1);
  });

  it("dispose stops delivery for good", () => {
    const { c, r, ids } = setup([ev(1, 0), ev(2, 1000)]);
    r.play();
    c.advance(0);
    r.dispose();
    c.advance(5000);
    expect(ids()).toEqual([1]);
  });
});
