// k6 load for POST /jobs against the LOCAL stack. Two modes, picked with MODE:
//   MODE=rate   constant arrival rate (RATE requests/s for DURATION): "how fast can the API take jobs?"
//   MODE=burst  TOTAL jobs as fast as VUS virtual users can send them: fills the queue so the
//               workers' drain time shows engine throughput.
// Each request carries a unique Idempotency-Key, like a real client.
import http from "k6/http";
import { check } from "k6";

const MODE = __ENV.MODE || "rate";
const BASE = __ENV.BASE || "http://127.0.0.1:8000";

export const options = {
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)"], // k6 omits p99 unless asked
  ...(MODE === "burst"
    ? { scenarios: { burst: { executor: "shared-iterations", vus: Number(__ENV.VUS || 50), iterations: Number(__ENV.TOTAL || 2000), maxDuration: "120s" } } }
    : {
        scenarios: {
          rate: {
            executor: "constant-arrival-rate",
            rate: Number(__ENV.RATE || 100),
            timeUnit: "1s",
            duration: __ENV.DURATION || "20s",
            preAllocatedVUs: 100,
            maxVUs: 400,
          },
        },
      }),
};

export default function () {
  const key = `bench-${__ENV.RUN || "x"}-${__VU}-${__ITER}-${Date.now()}`;
  const res = http.post(`${BASE}/jobs`, JSON.stringify({ type: "llm.mock", payload: { prompt: "bench" } }), {
    headers: { "Content-Type": "application/json", "Idempotency-Key": key },
  });
  check(res, { "201": (r) => r.status === 201 });
}

export function handleSummary(data) {
  const d = data.metrics.http_req_duration.values;
  const out = {
    mode: MODE,
    requests: data.metrics.http_reqs.values.count,
    req_per_sec: Number(data.metrics.http_reqs.values.rate.toFixed(1)),
    failed_rate: data.metrics.http_req_failed.values.rate,
    p50_ms: Number((d.med ?? d["p(50)"]).toFixed(1)),
    p95_ms: Number(d["p(95)"].toFixed(1)),
    p99_ms: Number(d["p(99)"].toFixed(1)),
    dropped_iterations: data.metrics.dropped_iterations ? data.metrics.dropped_iterations.values.count : 0,
  };
  return { [__ENV.OUT || "/dev/stdout"]: JSON.stringify(out, null, 1) + "\n" };
}
