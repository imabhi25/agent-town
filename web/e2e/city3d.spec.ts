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
  // The page starts on a recording and pings /healthz; answering it is what flips it to live.
  await page.route("**/healthz", (r) => r.fulfill({ json: { status: "ok", live: true, sleeping: false } }));
  await page.route(/\/(chaos|dlq|tasks)\//, async (r) => {
    const req = r.request();
    posts.push({ url: new URL(req.url()).pathname, body: req.postDataJSON() });
    await r.fulfill({ json: { ok: true } });
  });
  await page.goto("/?view=3d");
  await connected;
  return { send: (...events) => ws.send(JSON.stringify({ type: "events", events })), posts };
}

const summary = (page: Page) => page.getByTestId("summary");
// The default Playwright config asks for reduced motion (so the older tests stay on the 2D map);
// these tests want the 3D scene, which ?view=3d forces. Software WebGL is slow, so waits are generous.
test.use({ viewport: { width: 1440, height: 900 } });

test("3D view: renders a canvas, follows events, and toggles to 2D and back", async ({ page }) => {
  const server = await openTown(page);
  await expect(page.getByTestId("map3d").locator("canvas")).toBeVisible({ timeout: 15_000 });
  await expect.poll(() => page.evaluate(() => window.__town3d?.frames ?? 0), { timeout: 15_000 }).toBeGreaterThan(5);
  await expect(summary(page)).toContainText("3 workers (0 busy)");

  server.send(event("job.started", { worker: "worker-1", job_id: "job-1" }));
  await expect(summary(page)).toContainText("3 workers (1 busy)");
  server.send(event("circuit.opened", { provider: "llm" }));
  await expect(summary(page)).toContainText("circuit tripped");
  server.send(event("circuit.closed", { provider: "llm" }));
  await expect(summary(page)).toContainText("circuit ok");
  server.send(event("task.awaiting_approval", { task_id: "t1", goal_id: "g1", name: "Review" }));
  await expect(page.getByRole("button", { name: "Approve" })).toBeVisible();

  // canvas pixels are not blank
  // (a WebGL canvas cannot be read back with drawImage, so look at a screenshot: a blank canvas is a
  // tiny PNG, a painted city is not)
  const shot = await page.getByTestId("map3d").screenshot();
  expect(shot.length).toBeGreaterThan(30_000);

  await page.getByRole("button", { name: "2D", exact: true }).click();
  await expect(page.getByTestId("map")).toBeVisible();
  await expect(page.getByTestId("map3d")).toHaveCount(0);
  await expect(summary(page)).toContainText("workers");
  await page.getByRole("button", { name: "3D", exact: true }).click();
  await expect(page.getByTestId("map3d").locator("canvas")).toBeVisible({ timeout: 15_000 });
});

test("3D view: clicking a robot asks the server to kill that worker", async ({ page }) => {
  const server = await openTown(page);
  await expect(page.getByTestId("map3d").locator("canvas")).toBeVisible({ timeout: 15_000 });
  await expect.poll(() => page.evaluate(() => window.__town3d?.workerScreen?.("worker-2") ?? null), { timeout: 15_000 }).not.toBeNull();
  await page.waitForTimeout(600); // let the pop-in finish so the robot is full size
  const at = await page.evaluate(() => window.__town3d!.workerScreen!("worker-2")!);
  await page.mouse.click(at.x, at.y);
  await expect.poll(() => server.posts.map((p) => p.url)).toContain("/chaos/kill");
  expect(server.posts[0].body).toEqual({ worker_id: "worker-2" });
  server.send(event("worker.killed", { worker: "worker-2" }));
  await expect(summary(page)).toContainText("2 workers");
});
