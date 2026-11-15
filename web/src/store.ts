// The event store is a pure function: (state, message) -> (state, needSnapshot).
// No sockets, no timers, no React, so every rule below is unit-tested directly.
//
// Rules:
//  1. Deduplicate by event id. The stream is at-least-once, and a snapshot and the live feed can
//     overlap, so the same event can arrive twice.
//  2. Order by (ts, id), not arrival order. Two transactions that commit in the opposite order
//     of their ids can be published out of order (see app/event_publisher.py), and a batch can
//     hold events in any order. Time first, id as the tie-break.
//  3. Ask for a fresh snapshot when we KNOW we missed events: the server says `dropped`
//     (our queue overflowed). A reconnect is handled by the connection layer. We do not detect
//     gaps from id holes: Postgres ids skip numbers when a transaction rolls back.
import type { ServerMessage, Snapshot, TownEvent } from "./types";

export const MAX_EVENTS = 500;

export interface StoreState {
  snapshot: Snapshot | null;
  events: TownEvent[]; // sorted by (ts, id), newest last, at most MAX_EVENTS
  seen: Set<number>; // ids of the events in `events`
  gaps: number; // how many gaps we have detected (for the debug view)
}

export const initialState: StoreState = { snapshot: null, events: [], seen: new Set(), gaps: 0 };

export function compareEvents(a: TownEvent, b: TownEvent): number {
  const ta = Date.parse(a.ts);
  const tb = Date.parse(b.ts);
  return ta !== tb ? ta - tb : a.id - b.id;
}

function merge(existing: TownEvent[], seen: Set<number>, incoming: TownEvent[]) {
  const fresh: TownEvent[] = [];
  const ids = new Set(seen);
  for (const e of incoming) {
    if (ids.has(e.id)) continue; // also drops duplicates inside the batch itself
    ids.add(e.id);
    fresh.push(e);
  }
  if (fresh.length === 0) return { events: existing, seen, added: 0 };
  const events = [...existing, ...fresh].sort(compareEvents).slice(-MAX_EVENTS);
  // Rebuild `seen` from what we keep, so it cannot grow without bound.
  return { events, seen: new Set(events.map((e) => e.id)), added: fresh.length };
}

export function reduce(
  state: StoreState,
  msg: ServerMessage,
): { state: StoreState; needSnapshot: boolean } {
  if (msg.type === "snapshot") {
    // A snapshot is the truth for the world (goals, tasks, workers, queue, stats). Its events are
    // MERGED into the log, not swapped in: a /state response can arrive after newer live events,
    // and replacing the log would throw those away. Overlap is dropped by id.
    const { events, seen } = merge(state.events, state.seen, msg.events);
    return { state: { ...state, snapshot: msg, events, seen }, needSnapshot: false };
  }
  const { events, seen } = merge(state.events, state.seen, msg.events);
  const gap = (msg.dropped ?? 0) > 0;
  return {
    state: { ...state, events, seen, gaps: state.gaps + (gap ? 1 : 0) },
    needSnapshot: gap,
  };
}
