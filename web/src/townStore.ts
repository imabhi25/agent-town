// The one place the page's event store lives. Live events (WebSocket) and recorded events
// (replay) are both pushed in through `dispatch`, so the map cannot tell them apart.
// Switching source = `reset()` then feed the new source; events of two sources never mix.
import { initialState, reduce, type StoreState } from "./store";
import type { ServerMessage } from "./types";

export type Status = "idle" | "connecting" | "open" | "closed" | "replay";
export type View = StoreState & { status: Status };

export class TownStore {
  state: StoreState = initialState;
  status: Status = "idle";
  // Rebuilt on every change so useSyncExternalStore sees a new reference.
  view: View = { ...initialState, status: "idle" };
  private listeners = new Set<() => void>();

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };
  getView = () => this.view;

  /** Feed one message through the reducer. Returns true when the source should refetch /state. */
  dispatch(msg: ServerMessage): boolean {
    const { state, needSnapshot } = reduce(this.state, msg);
    this.state = state;
    this.emit();
    return needSnapshot;
  }

  setStatus(status: Status) {
    if (status === this.status) return;
    this.status = status;
    this.emit();
  }

  /** Forget everything: used when the source changes (or a replay restarts). */
  reset(status: Status = "idle") {
    this.state = initialState;
    this.status = status;
    this.emit();
  }

  private emit() {
    this.view = { ...this.state, status: this.status };
    this.listeners.forEach((fn) => fn());
  }
}
