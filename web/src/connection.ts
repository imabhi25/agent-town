// Glue between the network and the pure store. Everything that touches the outside world is
// injected, so tests can drive it with a fake socket and a fake fetch.
import { reduce, initialState, type StoreState } from "./store";
import type { ServerMessage, Snapshot } from "./types";

export type Status = "connecting" | "open" | "closed";

export interface SocketLike {
  onopen: (() => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
  close(): void;
}

export interface ConnectionDeps {
  apiUrl: string; // e.g. http://localhost:8000
  openSocket: (url: string) => SocketLike;
  fetchJson: (url: string) => Promise<unknown>;
  setTimer: (fn: () => void, ms: number) => unknown;
  clearTimer: (t: unknown) => void;
}

export const wsUrl = (apiUrl: string) => apiUrl.replace(/^http/, "ws") + "/ws";

export class Connection {
  state: StoreState = initialState;
  status: Status = "connecting";
  private socket: SocketLike | null = null;
  private retryTimer: unknown = null;
  private attempt = 0;
  private refreshing = false;
  private stopped = false;
  private listeners = new Set<() => void>();
  // Rebuilt on every change so useSyncExternalStore sees a new reference (status included).
  view: StoreState & { status: Status } = { ...initialState, status: "connecting" };

  constructor(private deps: ConnectionDeps) {}

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };
  getView = () => this.view;

  start() {
    this.stopped = false;
    this.open();
  }

  stop() {
    this.stopped = true;
    if (this.retryTimer !== null) this.deps.clearTimer(this.retryTimer);
    this.socket?.close();
  }

  private emit() {
    this.view = { ...this.state, status: this.status };
    this.listeners.forEach((fn) => fn());
  }

  private open() {
    this.status = "connecting";
    const socket = this.deps.openSocket(wsUrl(this.deps.apiUrl));
    this.socket = socket;
    // A reconnect needs no extra request: the server sends a snapshot first on every new
    // connection (app/main.py), and the store merges it. Only a gap on a live socket needs one.
    socket.onopen = () => {
      this.attempt = 0;
      this.status = "open";
      this.emit();
    };
    socket.onmessage = (e) => this.handle(JSON.parse(e.data) as ServerMessage);
    socket.onclose = () => {
      if (this.socket !== socket) return; // an old socket closing late
      this.status = "closed";
      this.emit();
      if (!this.stopped) this.scheduleReconnect();
    };
    socket.onerror = () => socket.close();
    this.emit();
  }

  private scheduleReconnect() {
    // Exponential backoff 1s, 2s, 4s ... capped at 10s, so a down server is not hammered.
    const delay = Math.min(10_000, 1000 * 2 ** this.attempt++);
    this.retryTimer = this.deps.setTimer(() => this.open(), delay);
  }

  private handle(msg: ServerMessage) {
    const { state, needSnapshot } = reduce(this.state, msg);
    this.state = state;
    this.emit();
    if (needSnapshot) void this.refreshSnapshot();
  }

  /** GET /state. One request at a time: more gaps while one is in flight are covered by it. */
  async refreshSnapshot() {
    if (this.refreshing) return;
    this.refreshing = true;
    try {
      const snap = (await this.deps.fetchJson(`${this.deps.apiUrl}/state`)) as Snapshot;
      this.handle(snap);
    } catch {
      // Leave the gap; the next dropped batch or reconnect asks again.
    } finally {
      this.refreshing = false;
    }
  }
}
