// Calls to the backend's control endpoints. Every call resolves to {ok, message} instead of
// throwing, so the UI can show one line of feedback ("live cap reached", "Too many requests...").
export interface ApiResult {
  ok: boolean;
  status: number;
  message: string;
}

export interface Api {
  submitGoal(prompt: string): Promise<ApiResult>;
  killWorker(workerId?: string): Promise<ApiResult>;
  startWorker(): Promise<ApiResult>;
  setFailureRate(rate: number): Promise<ApiResult>;
  flood(count: number): Promise<ApiResult>;
  replay(jobId: string): Promise<ApiResult>;
  approve(taskId: string): Promise<ApiResult>;
  reject(taskId: string): Promise<ApiResult>;
}

export function createApi(baseUrl: string, fetchFn: typeof fetch = fetch): Api {
  async function post(path: string, body?: unknown): Promise<ApiResult> {
    try {
      const r = await fetchFn(baseUrl + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (r.ok) return { ok: true, status: r.status, message: "" };
      let detail = "";
      try {
        const j = (await r.json()) as { detail?: unknown };
        detail = typeof j.detail === "string" ? j.detail : "";
      } catch {
        /* not JSON */
      }
      return { ok: false, status: r.status, message: detail || `Request failed (${r.status})` };
    } catch {
      return { ok: false, status: 0, message: "Cannot reach the backend." };
    }
  }
  return {
    submitGoal: (prompt) => post("/goals", { prompt }),
    killWorker: (id) => post("/chaos/kill", id ? { worker_id: id } : {}),
    startWorker: () => post("/chaos/start", {}),
    setFailureRate: (rate) => post("/chaos/mock", { failure_rate: rate }),
    flood: (count) => post("/chaos/flood", { count }),
    replay: (id) => post(`/dlq/${encodeURIComponent(id)}/replay`),
    approve: (id) => post(`/tasks/${encodeURIComponent(id)}/approve`),
    reject: (id) => post(`/tasks/${encodeURIComponent(id)}/reject`),
  };
}

/** What GET /config says. `planner` decides whether free-text goals are offered at all. */
export interface PublicConfig {
  planner: "fixed" | "gemini";
}

/** Asks the backend which planner it runs. Anything unexpected counts as "fixed" (box stays hidden). */
export function parseConfig(body: unknown): PublicConfig {
  const planner = (body as { planner?: unknown } | null)?.planner;
  return { planner: planner === "gemini" ? "gemini" : "fixed" };
}
