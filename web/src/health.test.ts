import { describe, expect, it } from "vitest";
import { HealthMonitor, OFFLINE_POLL_MS, PILL, WAKE_POLL_MS, WAKE_TIMEOUT_MS, type HealthResult } from "./health";

function setup(answer: () => HealthResult | "throw" = () => ({ ok: false })) {
  let t = 0;
  let id = 1;
  const timers = new Map<number, { at: number; fn: () => void }>();
  let pings = 0;
  const m = new HealthMonitor({
    ping: async () => {
      pings++;
      const a = answer();
      if (a === "throw") throw new Error("network");
      return a;
    },
    now: () => t,
    setTimer: (fn, ms) => (timers.set(id, { at: t + ms, fn }), id++),
    clearTimer: (x) => void timers.delete(x as number),
  });
  const flush = () => new Promise((r) => setTimeout(r, 0));
  return {
    m,
    pings: () => pings,
    async advance(ms: number) {
      const end = t + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, x]) => x.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]);
        t = due[1].at;
        due[1].fn();
        await flush();
      }
      t = end;
    },
    flush,
  };
}

describe("HealthMonitor", () => {
  it("has the three pill texts", () => {
    expect(PILL).toEqual({
      waking: "Recorded Trace (Waking Live Backend...)",
      connected: "Live Backend Connected",
      offline: "Recorded Trace (Live Backend Offline)",
    });
  });

  it("starts waking and pings at once", async () => {
    const { m, pings, flush } = setup();
    m.start();
    expect(m.state).toBe("waking");
    await flush();
    expect(pings()).toBe(1);
    expect(m.state).toBe("waking");
  });

  it("becomes connected as soon as the backend answers, and stops polling", async () => {
    let up = false;
    const { m, pings, advance, flush } = setup(() => ({ ok: up }));
    m.start();
    await flush();
    up = true;
    await advance(WAKE_POLL_MS);
    expect(m.state).toBe("connected");
    const n = pings();
    await advance(120_000);
    expect(pings()).toBe(n);
  });

  it("polls every 5s while waking, then goes offline after 60s", async () => {
    const { m, pings, advance, flush } = setup();
    m.start();
    await flush();
    await advance(WAKE_TIMEOUT_MS - 1);
    expect(m.state).toBe("waking");
    expect(pings()).toBe(12); // t=0,5,...,55
    await advance(WAKE_POLL_MS); // the ping at t=60 fails: time is up
    expect(m.state).toBe("offline");
  });

  it("keeps retrying every 15s when offline and connects when it comes back", async () => {
    let up = false;
    const { m, pings, advance, flush } = setup(() => ({ ok: up }));
    m.start();
    await flush();
    await advance(WAKE_TIMEOUT_MS);
    expect(m.state).toBe("offline");
    const before = pings();
    await advance(OFFLINE_POLL_MS);
    expect(pings()).toBe(before + 1);
    expect(m.state).toBe("offline");
    up = true;
    await advance(OFFLINE_POLL_MS);
    expect(m.state).toBe("connected");
  });

  it("treats a thrown error like no answer", async () => {
    const { m, flush } = setup(() => "throw");
    m.start();
    await flush();
    expect(m.state).toBe("waking");
  });

  it("an answer with live=false is offline, with a reason, and keeps polling", async () => {
    const { m, advance, flush } = setup(() => ({ ok: true, live: false }));
    m.start();
    await flush();
    expect(m.state).toBe("offline");
    expect(m.reason).toMatch(/free-tier/);
    await advance(OFFLINE_POLL_MS);
    expect(m.state).toBe("offline");
  });

  it("markLost sends a connected monitor back to looking for the backend", async () => {
    let up = true;
    const { m, advance, flush } = setup(() => ({ ok: up }));
    m.start();
    await flush();
    expect(m.state).toBe("connected");
    up = false;
    m.markLost();
    expect(m.state).toBe("offline");
    await advance(OFFLINE_POLL_MS);
    expect(m.state).toBe("offline");
    up = true;
    await advance(OFFLINE_POLL_MS);
    expect(m.state).toBe("connected");
  });

  it("notifies subscribers on every state change", async () => {
    const { m, flush } = setup(() => ({ ok: true }));
    const seen: string[] = [];
    m.subscribe(() => seen.push(m.state));
    m.start();
    await flush();
    expect(seen).toEqual(["connected"]);
  });

  it("stop ends all polling", async () => {
    const { m, pings, advance, flush } = setup();
    m.start();
    await flush();
    m.stop();
    const n = pings();
    await advance(200_000);
    expect(pings()).toBe(n);
  });
});
