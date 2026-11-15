import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";

// Recorded presets (served from web/public/traces) and the switch to live.
const summary = (page: Page) => page.getByTestId("summary");
const pill = (page: Page) => page.getByTestId("pill");

const liveSnapshot = {
  type: "snapshot",
  goals: [],
  tasks: [],
  workers: [{ id: "live-1", job_id: null }, { id: "live-2", job_id: null }],
  queue: { depth: 0, queued: 0, retrying: 0, running: 0, succeeded: 0, dead: 0 },
  stats: {},
  dead_jobs: [],
  events: [],
  last_event_id: "0-0",
};

test.describe("backend unreachable", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/healthz", (r) => r.abort("connectionrefused"));
    await page.routeWebSocket(/\/ws$/, (ws) => ws.close());
  });

  test("works with no backend: plays a recorded preset and explains the disabled controls", async ({ page }) => {
    await page.goto("/");
    await expect(pill(page)).toHaveText("Recorded Trace (Waking Live Backend...)");
    await expect(page.getByTestId("mode")).toHaveText("Showing: recorded trace");
    // the first preset starts by itself: workers show up from the trace snapshot
    await expect(summary(page)).toContainText("3 workers");
    for (const name of ["Happy path", "Retries with backoff", "Chaos recovery", "DLQ, approval and replay"]) {
      await expect(page.getByRole("button", { name })).toBeVisible();
    }
    // speed up and wait for the end of the trace
    await page.getByRole("button", { name: "4x" }).click();
    await expect(summary(page)).toContainText("5 done", { timeout: 15_000 });
    await expect(page.getByRole("group", { name: "Playback" }).getByRole("button", { name: "Replay" })).toBeVisible();

    // live-only controls are disabled, with the reason
    await expect(page.getByRole("textbox")).toBeDisabled();
    await expect(page.getByRole("button", { name: "Kill worker" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Live", exact: true })).toBeDisabled();
    await expect(page.getByText(/live backend is waking up/).first()).toBeVisible();
    await page.screenshot({ path: "test-results/recorded-happy-path.png" });
  });

  test("the DLQ preset shows a dead job, then the replay succeeds", async ({ page }) => {
    test.setTimeout(60_000); // ~540 software-rendered frames are simulated on a slow runner
    // The page clock is frozen and driven by the test, so "5.5 seconds of trace" is exact, not a
    // race against a slow machine.
    await page.clock.install({ time: new Date("2026-06-01T00:00:00Z") });
    await page.clock.pauseAt(new Date("2026-06-01T00:00:01Z"));
    await page.goto("/");
    await page.getByRole("button", { name: "DLQ, approval and replay" }).click();
    await expect(page.getByText("0/39 events")).toBeVisible(); // loaded, waiting for the clock
    await page.clock.runFor(5500); // the doomed job reaches the DLQ at 4.97s
    await expect(summary(page)).toContainText("1 dead");
    await expect(summary(page)).toContainText("1 awaiting approval");
    // the approval card and the Replay button are there but cannot be clicked on a recording
    await expect(page.getByRole("button", { name: "Approve" })).toBeDisabled();
    await expect(page.getByRole("list").getByRole("button", { name: "Replay" })).toBeDisabled();
    await page.clock.runFor(3600); // approved at 7.5s, replayed at 8.5s, succeeded at 9.0s
    await expect(summary(page)).toContainText("0 dead");
    await expect(summary(page)).not.toContainText("awaiting approval");
    await expect(page.getByRole("group", { name: "Playback" }).getByRole("button", { name: "Replay" })).toBeVisible();
  });

  test("pause stops the clock, restart starts over", async ({ page }) => {
    await page.goto("/");
    const progress = page.getByText(/\d+\/\d+ events/);
    await expect(progress).not.toHaveText(/^0\//);
    await page.getByRole("button", { name: "Pause" }).click();
    const frozen = await progress.textContent();
    await page.waitForTimeout(800);
    expect(await progress.textContent()).toBe(frozen);
    await page.getByRole("button", { name: "Restart" }).click();
    await expect(progress).toHaveText(/^\d+\/28 events/);
  });

  test("goes to 'Live Backend Offline' after 60 seconds without an answer", async ({ page }) => {
    await page.clock.install();
    await page.goto("/");
    await expect(pill(page)).toHaveText("Recorded Trace (Waking Live Backend...)");
    // Each ping is a real (failed) request, so advance the page clock 5s at a time until the
    // 60s wake window has passed.
    await expect
      .poll(
        async () => {
          await page.clock.fastForward(5_000);
          return pill(page).textContent();
        },
        { intervals: [100], timeout: 20_000 },
      )
      .toBe("Recorded Trace (Live Backend Offline)");
    await expect(summary(page)).toContainText("workers"); // the recording is still there
  });

  test("a preset can be chosen with the keyboard", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Retries with backoff" }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("button", { name: "Retries with backoff" })).toHaveAttribute("aria-pressed", "true");
  });

  test("mobile: no horizontal scroll with the preset bar", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto("/");
    await expect(summary(page)).toContainText("workers");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await page.screenshot({ path: "test-results/recorded-mobile.png", fullPage: true });
  });
});

test.describe("backend wakes up", () => {
  async function wake(page: Page, opts: { goals?: number } = {}) {
    let ws!: WebSocketRoute;
    const connected = new Promise<void>((resolve) => {
      void page.routeWebSocket(/\/ws$/, (route) => {
        ws = route;
        route.send(JSON.stringify(liveSnapshot));
        resolve();
      });
    });
    // The host is "asleep": /healthz hangs for a moment, then answers.
    await page.route("**/healthz", async (r) => {
      await new Promise((x) => setTimeout(x, 1200));
      await r.fulfill({ json: { status: "ok", live: true, sleeping: false } });
    });
    const posts: string[] = [];
    await page.route("**/goals", async (r) => {
      posts.push(r.request().postData() ?? "");
      if (opts.goals === 429) return r.fulfill({ status: 429, json: { detail: "Daily live-goal cap reached." } });
      await r.fulfill({ status: 201, json: { id: "g1", status: "planning" } });
    });
    return { connected, send: (m: unknown) => ws.send(JSON.stringify(m)), close: () => ws.close(), posts };
  }

  test("flips from a recording to the live backend and clears the recorded events", async ({ page }) => {
    const live = await wake(page);
    await page.goto("/");
    await expect(pill(page)).toHaveText("Recorded Trace (Waking Live Backend...)");
    await expect(summary(page)).toContainText("3 workers"); // the recording, while waiting
    await expect(pill(page)).toHaveText("Live Backend Connected");
    await live.connected;
    await expect(page.getByTestId("mode")).toHaveText("Showing: live");
    // only the live snapshot's two workers: nothing from the recording is left
    await expect(summary(page)).toContainText("2 workers");
    await expect(summary(page)).toContainText("0 done");
    // live controls are on now
    await expect(page.getByRole("textbox")).toBeEnabled();
    await expect(page.getByRole("button", { name: "Kill worker" })).toBeEnabled();

    live.send({ type: "events", events: [{ id: 9001, stream_id: "9001-0", type: "job.started", payload: { worker: "live-1", job_id: "j" }, ts: new Date().toISOString() }] });
    await expect(summary(page)).toContainText("(1 busy)");

    // the visitor goes back to a recording: the live events are gone
    await page.getByRole("button", { name: "Recorded", exact: true }).click();
    await expect(page.getByTestId("mode")).toHaveText("Showing: recorded trace");
    await expect(summary(page)).toContainText("3 workers");
    await expect(page.getByRole("textbox")).toBeDisabled();
    await page.screenshot({ path: "test-results/live-then-recorded.png" });
  });

  test("a typed prompt is sent; a 429 shows a friendly message", async ({ page }) => {
    const live = await wake(page, { goals: 429 });
    await page.goto("/");
    await live.connected;
    await expect(page.getByTestId("mode")).toHaveText("Showing: live");
    await page.getByRole("textbox").fill("Plan a weekend in Lisbon");
    await page.getByRole("button", { name: "Plan it" }).click();
    await expect(page.getByTestId("notice")).toContainText("Daily live-goal cap reached.");
    await expect(page.getByTestId("notice")).toContainText("Pick a preset");
    expect(live.posts).toEqual(['{"prompt":"Plan a weekend in Lisbon"}']);
  });

  test("falls back to a recording when the live socket drops", async ({ page }) => {
    const live = await wake(page);
    await page.goto("/");
    await live.connected;
    await expect(page.getByTestId("mode")).toHaveText("Showing: live");
    live.close();
    await expect(page.getByTestId("notice")).toContainText("live backend disconnected");
    await expect(pill(page)).toHaveText("Recorded Trace (Live Backend Offline)");
    await expect(page.getByTestId("mode")).toHaveText("Showing: recorded trace");
    await expect(summary(page)).toContainText("3 workers");
  });

  test("choosing a preset before the backend answers keeps the recording", async ({ page }) => {
    await wake(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Chaos recovery" }).click();
    await expect(pill(page)).toHaveText("Live Backend Connected");
    await expect(page.getByTestId("mode")).toHaveText("Showing: recorded trace");
    await expect(page.getByRole("button", { name: "Live", exact: true })).toBeEnabled();
  });
});
