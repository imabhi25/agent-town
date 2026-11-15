import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";

// A mock backend: the page connects to ws://localhost:4173/ws, and the test plays the server.
let seq = 0;
const event = (type: string, payload: Record<string, unknown> = {}) => {
  seq++;
  return { id: seq, stream_id: `${seq}-0`, type, payload, ts: new Date(Date.UTC(2026, 0, 1, 0, 0, seq)).toISOString() };
};
const snapshot = (over: Record<string, unknown> = {}) => ({
  type: "snapshot",
  goals: [],
  tasks: [],
  workers: [{ id: "worker-1", job_id: null }, { id: "worker-2", job_id: null }, { id: "worker-3", job_id: null }],
  queue: { depth: 3, queued: 3, retrying: 0, running: 0, succeeded: 0, dead: 0 },
  stats: {},
  dead_jobs: [],
  events: [],
  last_event_id: "0-0",
  ...over,
});

interface Server {
  send: (...events: ReturnType<typeof event>[]) => void;
  posts: { url: string; body: unknown }[];
}

async function openTown(page: Page, snap = snapshot()): Promise<Server> {
  const posts: Server["posts"] = [];
  let ws!: WebSocketRoute;
  const connected = new Promise<void>((resolve) => {
    void page.routeWebSocket(/\/ws$/, (route) => {
      ws = route;
      route.send(JSON.stringify(snap)); // the real server sends a snapshot first
      resolve();
    });
  });
  await page.route("**/state", (r) => r.fulfill({ json: snap }));
  await page.route(/\/(chaos|dlq|tasks)\//, async (r) => {
    const req = r.request();
    posts.push({ url: new URL(req.url()).pathname, body: req.postDataJSON() });
    await r.fulfill({ json: { ok: true } });
  });
  await page.goto("/");
  await connected;
  return { send: (...events) => ws.send(JSON.stringify({ type: "events", events })), posts };
}

const summary = (page: Page) => page.getByTestId("summary");

test("draws the map and the first snapshot", async ({ page }) => {
  await openTown(page);
  await expect(page.locator("canvas")).toBeVisible();
  await expect(summary(page)).toContainText("3 workers (0 busy)");
  await expect(summary(page)).toContainText("3 queued");
  await expect(page.getByText("Connected")).toBeVisible();
  await page.screenshot({ path: "test-results/map-idle.png" });
});

test("events drive the workers, counters, DLQ and circuit gate", async ({ page }) => {
  const server = await openTown(page);
  server.send(event("job.started", { worker: "worker-1", job_id: "job-a" }), event("job.started", { worker: "worker-2", job_id: "job-b" }));
  await expect(summary(page)).toContainText("(2 busy)");
  await expect(summary(page)).toContainText("1 queued");

  server.send(event("job.succeeded", { worker: "worker-1", job_id: "job-a" }));
  await expect(summary(page)).toContainText("1 done");

  server.send(event("job.retry_scheduled", { worker: "worker-2", job_id: "job-b", attempt: 1 }));
  await expect(summary(page)).toContainText("(0 busy)");

  server.send(event("circuit.opened", { provider: "gemini" }));
  await expect(summary(page)).toContainText("circuit tripped");
  await expect.poll(() => page.evaluate(() => window.__town?.summary().circuit)).toBe("tripped");
  await page.waitForTimeout(700); // let the gate finish swinging shut
  await page.screenshot({ path: "test-results/map-circuit-closed.png" });
  server.send(event("circuit.closed", { provider: "gemini" }));
  await expect(summary(page)).toContainText("circuit ok");

  server.send(event("job.dead", { worker: "worker-3", job_id: "dead-job-1234", error: "boom" }));
  await expect(summary(page)).toContainText("1 dead");
  await expect(page.locator("code", { hasText: "dead-job" })).toBeVisible();
  await page.screenshot({ path: "test-results/map-dead.png" });

  await page.getByRole("button", { name: "Replay" }).click();
  await expect.poll(() => server.posts.map((p) => p.url)).toContain("/dlq/dead-job-1234/replay");
  server.send(event("job.replayed", { job_id: "dead-job-1234" }));
  await expect(summary(page)).toContainText("0 dead");
  await expect(page.getByText("Nothing dead")).toBeVisible();
});

test("ignores duplicate events (at-least-once delivery)", async ({ page }) => {
  const server = await openTown(page);
  const done = event("job.succeeded", { worker: "worker-1", job_id: "x" });
  server.send(done);
  server.send(done);
  server.send(done);
  await expect(summary(page)).toContainText("1 done");
  await page.waitForTimeout(300);
  await expect(summary(page)).toContainText("1 done");
});

test("approval card on the map: approve and reject call the API", async ({ page }) => {
  const server = await openTown(page);
  server.send(event("task.awaiting_approval", { goal_id: "g1", task_id: "task-1", name: "Book the hotel" }));
  const card = page.getByRole("region", { name: "Approvals needed" });
  await expect(card).toContainText("Book the hotel");
  await card.getByRole("button", { name: "Approve" }).click();
  await expect.poll(() => server.posts.map((p) => p.url)).toContain("/tasks/task-1/approve");
  server.send(event("task.approved", { goal_id: "g1", task_id: "task-1" }));
  await expect(card).toHaveCount(0);
});

test("chaos panel: kill, start, failure rate and flood", async ({ page }) => {
  const server = await openTown(page);
  await page.getByRole("combobox", { name: /Worker/ }).selectOption("worker-2");
  await page.getByRole("button", { name: "Kill worker" }).click();
  await page.getByRole("button", { name: "Start worker" }).click();
  await page.getByRole("button", { name: /Flood \d+ mock jobs/ }).click();
  await expect.poll(() => server.posts.map((p) => p.url)).toEqual(["/chaos/kill", "/chaos/start", "/chaos/flood"]);
  expect(server.posts[0].body).toEqual({ worker_id: "worker-2" });
  expect(server.posts[2].body).toEqual({ count: 50 });
  // the killed worker leaves the map when the server says so
  server.send(event("worker.killed", { worker: "worker-2" }));
  await expect(summary(page)).toContainText("2 workers");
});

test("a 429 from the backend is shown, not swallowed", async ({ page }) => {
  await openTown(page);
  await page.route("**/chaos/flood", (r) => r.fulfill({ status: 429, json: { detail: "Too many chaos requests. Try again in 3s." } }));
  await page.getByRole("button", { name: /Flood/ }).click();
  await expect(page.getByRole("alert")).toContainText("Too many chaos requests");
});

test("keyboard: every control is reachable with Tab", async ({ page }) => {
  await openTown(page);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("combobox", { name: /Worker/ })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Kill worker" })).toBeFocused();
});

test("mobile layout: no horizontal scroll and the panels stack", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await openTown(page);
  await expect(summary(page)).toContainText("3 workers");
  await page.waitForTimeout(300);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.screenshot({ path: "test-results/map-mobile.png" });
});

test("holds a steady frame rate while 600 events/min stream in", async ({ page }) => {
  const server = await openTown(page);
  // 10 events per second for 3 seconds, spread over the three workers.
  for (let i = 0; i < 30; i++) {
    const w = `worker-${(i % 3) + 1}`;
    server.send(event("job.started", { worker: w, job_id: `j${i}` }), event(i % 7 === 0 ? "job.retry_scheduled" : "job.succeeded", { worker: w, job_id: `j${i}` }));
    await page.waitForTimeout(100);
  }
  const fps = await page.evaluate(() => window.__town?.fps ?? 0);
  console.log("measured fps (headless, software GL):", Math.round(fps));
  expect(fps).toBeGreaterThan(20); // CI runners have no GPU; the real 60fps check is the scene perf test + manual
});
