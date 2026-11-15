// Glue between the network and the pure store. Everything that touches the outside world is
// injected, so tests can drive it with a fake socket and a fake fetch.
import { TownStore } from "./townStore";
import type { ServerMessage, Snapshot } from "./types";

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
  private socket: SocketLike | null = null;
  private retryTimer: unknown = null;
  private attempt = 0;
  private refreshing = false;
  private stopped = false;

  /** The store is shared with the replay engine: the map reads it, whoever fills it. */
  constructor(
    private deps: ConnectionDeps,
    readonly store: TownStore = new TownStore(),
  ) {}

  get view() {
    return this.store.view;
  }

  start() {
    this.stopped = false;
    this.open();
  }

  stop() {
    this.stopped = true;
    if (this.retryTimer !== null) this.deps.clearTimer(this.retryTimer);
    const socket = this.socket;
    this.socket = null; // so its late onclose is ignored (it must not flip the status or retry)
    socket?.close();
  }

  private open() {
    this.store.setStatus("connecting");
    const socket = this.deps.openSocket(wsUrl(this.deps.apiUrl));
    this.socket = socket;
    // A reconnect needs no extra request: the server sends a snapshot first on every new
    // connection (app/main.py), and the store merges it. Only a gap on a live socket needs one.
    socket.onopen = () => {
      this.attempt = 0;
      this.store.setStatus("open");
    };
    socket.onmessage = (e) => {
      if (this.socket !== socket) return; // stopped (e.g. the page switched to a recording)
      this.handle(JSON.parse(e.data) as ServerMessage);
    };
    socket.onclose = () => {
      if (this.socket !== socket) return; // an old socket closing late
      this.store.setStatus("closed");
      if (!this.stopped) this.scheduleReconnect();
    };
    socket.onerror = () => socket.close();
  }

  private scheduleReconnect() {
    // Exponential backoff 1s, 2s, 4s ... capped at 10s, so a down server is not hammered.
    const delay = Math.min(10_000, 1000 * 2 ** this.attempt++);
    this.retryTimer = this.deps.setTimer(() => this.open(), delay);
  }

  private handle(msg: ServerMessage) {
    if (this.store.dispatch(msg)) void this.refreshSnapshot();
  }

  /** GET /state. One request at a time: more gaps while one is in flight are covered by it. */
  async refreshSnapshot() {
    if (this.refreshing) return;
    this.refreshing = true;
    try {
      const snap = (await this.deps.fetchJson(`${this.deps.apiUrl}/state`)) as Snapshot;
      if (!this.stopped) this.handle(snap); // a late answer must not leak into another source
    } catch {
      // Leave the gap; the next dropped batch or reconnect asks again.
    } finally {
      this.refreshing = false;
    }
  }
}
