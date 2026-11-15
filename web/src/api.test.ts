import { describe, expect, it, vi } from "vitest";
import { createApi } from "./api";

const reply = (status: number, body?: unknown) =>
  vi.fn().mockResolvedValue(new Response(body === undefined ? null : JSON.stringify(body), { status }));

describe("createApi", () => {
  it("posts JSON to the right endpoints", async () => {
    const f = reply(200, {});
    const api = createApi("http://api", f);
    await api.submitGoal("plan a trip");
    await api.killWorker("worker-2");
    await api.killWorker();
    await api.setFailureRate(0.3);
    await api.flood(40);
    await api.replay("a/b");
    await api.approve("t1");
    await api.reject("t2");
    const calls = f.mock.calls.map(([url, init]) => [url, init.method, init.body]);
    expect(calls).toEqual([
      ["http://api/goals", "POST", '{"prompt":"plan a trip"}'],
      ["http://api/chaos/kill", "POST", '{"worker_id":"worker-2"}'],
      ["http://api/chaos/kill", "POST", "{}"],
      ["http://api/chaos/mock", "POST", '{"failure_rate":0.3}'],
      ["http://api/chaos/flood", "POST", '{"count":40}'],
      ["http://api/dlq/a%2Fb/replay", "POST", undefined],
      ["http://api/tasks/t1/approve", "POST", undefined],
      ["http://api/tasks/t2/reject", "POST", undefined],
    ]);
  });
  it("turns a 429 with a detail into a readable message", async () => {
    const r = await createApi("http://api", reply(429, { detail: "Daily limit reached." })).flood(5);
    expect(r).toEqual({ ok: false, status: 429, message: "Daily limit reached." });
  });
  it("falls back to a generic message when the body is not JSON", async () => {
    const r = await createApi("http://api", reply(502)).startWorker();
    expect(r).toMatchObject({ ok: false, status: 502, message: "Request failed (502)" });
  });
  it("reports a network failure instead of throwing", async () => {
    const f = vi.fn().mockRejectedValue(new TypeError("fetch failed"));
    expect(await createApi("http://api", f).startWorker()).toEqual({
      ok: false, status: 0, message: "Cannot reach the backend.",
    });
  });
  it("reports success with no message", async () => {
    expect(await createApi("http://api", reply(200, {})).startWorker()).toEqual({ ok: true, status: 200, message: "" });
  });
});
