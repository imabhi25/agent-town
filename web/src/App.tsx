// Debug view only: proves the store works against the live backend. The city map is a later step.
import { useEffect, useMemo, useSyncExternalStore } from "react";
import { Connection } from "./connection";

const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";

export function App() {
  const conn = useMemo(
    () =>
      new Connection({
        apiUrl: API_URL,
        openSocket: (url) => new WebSocket(url) as never,
        fetchJson: async (url) => {
          const r = await fetch(url);
          if (!r.ok) throw new Error(String(r.status));
          return r.json();
        },
        setTimer: (fn, ms) => setTimeout(fn, ms),
        clearTimer: (t) => clearTimeout(t as number),
      }),
    [],
  );
  useEffect(() => {
    conn.start();
    return () => conn.stop();
  }, [conn]);
  const state = useSyncExternalStore(conn.subscribe, conn.getView);
  const snap = state.snapshot;

  return (
    <main style={{ fontFamily: "system-ui", padding: 16, maxWidth: 900, margin: "0 auto" }}>
      <h1>Agent Town (debug view)</h1>
      <p role="status">
        Connection: <strong>{state.status}</strong> · events: {state.events.length} · gaps:{" "}
        {state.gaps}
      </p>
      {snap ? (
        <p>
          Workers: {snap.workers.length} · queue depth: {snap.queue.depth} · goals:{" "}
          {snap.goals.length}
        </p>
      ) : (
        <p>Waiting for the first snapshot…</p>
      )}
      <ol style={{ fontFamily: "monospace", fontSize: 13, paddingLeft: 24 }}>
        {state.events
          .slice(-50)
          .reverse()
          .map((e) => (
            <li key={e.id}>
              {e.ts.slice(11, 23)} #{e.id} {e.type}
            </li>
          ))}
      </ol>
    </main>
  );
}
