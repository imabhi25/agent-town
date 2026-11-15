import { useEffect, useMemo, useSyncExternalStore } from "react";
import { createApi } from "./api";
import { CityMap } from "./CityMap";
import { Connection } from "./connection";
import "./styles.css";

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
  const api = useMemo(() => createApi(API_URL), []);
  useEffect(() => {
    conn.start();
    return () => conn.stop();
  }, [conn]);
  const view = useSyncExternalStore(conn.subscribe, conn.getView);

  return (
    <main>
      <header>
        <h1>Agent Town</h1>
        <span className={`badge ${view.status}`} role="status">
          {view.status === "open" ? "Connected" : view.status === "connecting" ? "Connecting…" : "Disconnected, retrying…"}
        </span>
      </header>
      <CityMap state={view} api={api} />
    </main>
  );
}
