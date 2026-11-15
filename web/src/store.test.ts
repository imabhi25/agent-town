import { describe, expect, it } from "vitest";
import { initialState, MAX_EVENTS, reduce } from "./store";
import type { EventBatch, Snapshot, TownEvent } from "./types";

const ev = (id: number, ts = `2026-01-01T00:00:${String(id % 60).padStart(2, "0")}.000+00:00`): TownEvent => ({
  id, stream_id: `${id}-0`, type: "job.started", payload: {}, ts,
});
const batch = (events: TownEvent[], dropped?: number): EventBatch => ({ type: "events", events, dropped });
const snapshot = (events: TownEvent[]): Snapshot => ({
  type: "snapshot", goals: [], tasks: [], workers: [], queue: {}, stats: {}, events, last_event_id: "0-0",
});
const ids = (s: { events: TownEvent[] }) => s.events.map((e) => e.id);

describe("dedupe by id", () => {
  it("drops an event seen in an earlier batch", () => {
    let { state } = reduce(initialState, batch([ev(1), ev(2)]));
    ({ state } = reduce(state, batch([ev(2), ev(3)])));
    expect(ids(state)).toEqual([1, 2, 3]);
  });
  it("drops duplicates inside one batch", () => {
    const { state } = reduce(initialState, batch([ev(1), ev(1), ev(1)]));
    expect(ids(state)).toEqual([1]);
  });
  it("drops snapshot events that overlap the live log", () => {
    let { state } = reduce(initialState, batch([ev(1), ev(2)]));
    ({ state } = reduce(state, snapshot([ev(1), ev(2), ev(3)])));
    expect(ids(state)).toEqual([1, 2, 3]);
  });
  it("keeps live events newer than a late snapshot", () => {
    let { state } = reduce(initialState, batch([ev(1), ev(5)]));
    ({ state } = reduce(state, snapshot([ev(1), ev(2)])));
    expect(ids(state)).toEqual([1, 2, 5]);
  });
});

describe("order by (created_at, id)", () => {
  it("sorts a batch by time, not arrival", () => {
    const { state } = reduce(initialState, batch([ev(3), ev(1), ev(2)]));
    expect(ids(state)).toEqual([1, 2, 3]);
  });
  it("uses id as the tie-break for equal timestamps", () => {
    const t = "2026-01-01T00:00:00.000+00:00";
    const { state } = reduce(initialState, batch([ev(9, t), ev(4, t), ev(7, t)]));
    expect(ids(state)).toEqual([4, 7, 9]);
  });
  it("puts a late older event before newer ones already shown", () => {
    let { state } = reduce(initialState, batch([ev(5)]));
    ({ state } = reduce(state, batch([ev(2)])));
    expect(ids(state)).toEqual([2, 5]);
  });
  it("time wins over id (a lower id can be newer)", () => {
    const { state } = reduce(initialState, batch([ev(1, "2026-01-01T00:00:09.000+00:00"), ev(2, "2026-01-01T00:00:01.000+00:00")]));
    expect(ids(state)).toEqual([2, 1]);
  });
});

describe("gaps and snapshots", () => {
  it("requests a snapshot when the server reports dropped events", () => {
    const r = reduce(initialState, batch([ev(1)], 7));
    expect(r.needSnapshot).toBe(true);
    expect(r.state.gaps).toBe(1);
  });
  it("does not request one for a normal batch, even with id holes", () => {
    const r = reduce(initialState, batch([ev(1), ev(10)]));
    expect(r.needSnapshot).toBe(false);
    expect(r.state.gaps).toBe(0);
  });
  it("a snapshot replaces the world and never asks for another", () => {
    const r = reduce(initialState, snapshot([]));
    expect(r.needSnapshot).toBe(false);
    expect(r.state.snapshot).not.toBeNull();
  });
  it("caps the log and the seen set", () => {
    const many = Array.from({ length: MAX_EVENTS + 50 }, (_, i) => ev(i + 1, new Date(i * 1000).toISOString()));
    const { state } = reduce(initialState, batch(many));
    expect(state.events).toHaveLength(MAX_EVENTS);
    expect(state.seen.size).toBe(MAX_EVENTS);
    expect(state.events[0].id).toBe(51); // the oldest were dropped
  });
});
