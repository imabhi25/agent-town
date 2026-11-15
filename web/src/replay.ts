// Replay engine: feeds a recorded trace into the SAME store the live WebSocket feeds.
//
// Timing comes from the recorded timestamps, never from invented delays: an event is delivered
// when (ts - first ts) / speed has passed. Events that are due at the same moment go out as one
// batch, like the server's batches. The map cannot tell a replay from a live feed.
import type { ServerMessage, Snapshot, TownEvent, Trace } from "./types";
import { compareEvents } from "./store";

export type PlayState = "idle" | "playing" | "paused" | "ended";
export const SPEEDS = [1, 2, 4] as const;

export interface ReplayDeps {
  now: () => number; // ms
  setTimer: (fn: () => void, ms: number) => unknown;
  clearTimer: (t: unknown) => void;
}

export interface ReplaySink {
  reset: () => void; // forget everything the store holds (restart)
  push: (msg: ServerMessage) => void;
}

/** Drop duplicate ids and sort by (ts, id): the same rules as the store, applied up front so the
 *  schedule below is built from a clean, ordered list. */
export function prepareEvents(events: TownEvent[]): TownEvent[] {
  const seen = new Set<number>();
  const unique = events.filter((e) => (seen.has(e.id) ? false : (seen.add(e.id), true)));
  return unique.sort(compareEvents);
}

export class Replayer {
  readonly events: TownEvent[];
  private offsets: number[]; // ms after the first event
  private snapshot: Snapshot;
  speed = 1;
  state: PlayState = "idle";
  next = 0; // index of the next event to deliver
  private anchorReal = 0; // real time when we last (re)started the clock
  private anchorPos = 0; // trace position (ms) at that moment
  private timer: unknown = null;

  constructor(
    trace: Trace,
    private sink: ReplaySink,
    private deps: ReplayDeps,
    private onChange: () => void = () => {},
  ) {
    this.events = prepareEvents(trace.events);
    const t0 = this.events.length ? Date.parse(this.events[0].ts) : 0;
    this.offsets = this.events.map((e) => Date.parse(e.ts) - t0);
    this.snapshot = trace.snapshot;
  }

  get duration(): number {
    return this.offsets.length ? this.offsets[this.offsets.length - 1] : 0;
  }

  /** Where the trace clock is now, in ms. */
  get position(): number {
    if (this.state !== "playing") return this.anchorPos;
    return Math.min(this.duration, this.anchorPos + (this.deps.now() - this.anchorReal) * this.speed);
  }

  play() {
    if (this.state === "playing") return;
    if (this.state === "ended") return this.restart();
    if (this.state === "idle") this.sink.push(this.snapshot); // the world before event 1
    this.state = "playing";
    this.anchorReal = this.deps.now();
    this.schedule();
    this.onChange();
  }

  pause() {
    if (this.state !== "playing") return;
    this.anchorPos = this.position; // freeze the clock where it is
    this.cancelTimer();
    this.state = "paused";
    this.onChange();
  }

  restart() {
    this.cancelTimer();
    this.sink.reset();
    this.next = 0;
    this.anchorPos = 0;
    this.state = "idle";
    this.play();
  }

  setSpeed(speed: number) {
    if (this.state === "playing") {
      this.anchorPos = this.position; // keep the position continuous across the change
      this.anchorReal = this.deps.now();
      this.speed = speed;
      this.schedule();
    } else {
      this.speed = speed;
    }
    this.onChange();
  }

  /** Stop for good (the page switched source). Delivers nothing more. */
  dispose() {
    this.cancelTimer();
    this.state = "paused";
  }

  private cancelTimer() {
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.timer = null;
  }

  private schedule() {
    this.cancelTimer();
    if (this.next >= this.events.length) return this.finish();
    const wait = (this.offsets[this.next] - this.position) / this.speed;
    this.timer = this.deps.setTimer(() => this.fire(), Math.max(0, wait));
  }

  private fire() {
    this.timer = null;
    // Everything that is due by now goes out as one batch. The first one always goes: this timer
    // was set for it, and a timer can fire a hair early.
    const due = this.position + 1;
    const batch = [this.events[this.next++]];
    while (this.next < this.events.length && this.offsets[this.next] <= due) {
      batch.push(this.events[this.next++]);
    }
    this.sink.push({ type: "events", events: batch });
    if (this.next >= this.events.length) this.finish();
    else this.schedule();
    this.onChange();
  }

  private finish() {
    this.anchorPos = this.duration;
    this.state = "ended";
    this.onChange();
  }
}
