// Records docs/demo.gif: a goal on the live map, a worker killed mid-job, recovery, approval.
//
// Mock planner and mock LLM only, so no API quota is used. Needs a fresh local stack whose
// timings are tuned so the whole story fits in about 20 seconds.
// Without a matching Playwright browser, set CHROMIUM_PATH to a Chromium binary. Setup:
//   psql ... -c "TRUNCATE goals, tasks, jobs, job_outputs, outbox, events RESTART IDENTITY CASCADE"
//   redis-cli FLUSHALL
//   PLANNER=gemini EMBEDDED_WORKERS=true WORKER_COUNT=4 GEMINI_API_KEY= \
//     MOCK_MIN_LATENCY=3.0 MOCK_MAX_LATENCY=3.5 \
//     HEARTBEAT_INTERVAL=0.5 HEARTBEAT_TTL=1.5 LEASE_SECONDS=2.5 RECOVERY_INTERVAL=0.5 \
//     uvicorn app.main:app --port 8000
//   cd web && npm run dev
// Then, from web/:
//   node scripts/record-demo.mjs /tmp/demo
//   ffmpeg -f concat -i /tmp/demo/frames.txt -ss 1.6 -vf "setpts=PTS/1.2,fps=10,scale=900:-1:\
//     flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];\
//     [b][p]paletteuse=dither=none:diff_mode=rectangle" ../docs/demo.gif
// (-ss skips the page load; setpts plays it at 1.2x so it fits in about 20 s.)
//
// The numbered captions are a DOM overlay added by this script only; the app does not show them.
import { mkdirSync, writeFileSync } from "node:fs";
import { chromium } from "playwright";

const out = process.argv[2] ?? "demo-recording";
const state = async () => (await fetch("http://localhost:8000/state")).json();
(async () => {
  const b = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH }); // unset = Playwright's own browser
  const vp = { width: 1280, height: 1300 };
  const ctx = await b.newContext({ viewport: vp });
  const p = await ctx.newPage();
  // CDP screencast instead of Playwright's recordVideo: its low-bitrate VP8 smears the 3D scene.
  // Frames are saved as JPEGs plus an ffconcat list that keeps their real timing.
  mkdirSync(out + '/frames', { recursive: true });
  const frames = [];
  const cdp = await ctx.newCDPSession(p);
  cdp.on('Page.screencastFrame', async ({ data, metadata, sessionId }) => {
    const file = `frames/${String(frames.length).padStart(5, '0')}.jpg`;
    writeFileSync(`${out}/${file}`, Buffer.from(data, 'base64'));
    frames.push({ file, t: metadata.timestamp });
    await cdp.send('Page.screencastFrameAck', { sessionId }).catch(() => {});
  });
  await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 92, maxWidth: vp.width, maxHeight: vp.height, everyNthFrame: 1 });
  const t0 = Date.now(); const mark = (m) => console.log(((Date.now()-t0)/1000).toFixed(1), m);
  const cap = (text) => p.evaluate((t) => {
    let el = document.getElementById('demo-cap');
    if (!el) { el = document.createElement('div'); el.id = 'demo-cap';
      el.style.cssText = 'position:fixed;left:50%;top:268px;transform:translateX(-50%);z-index:9999;background:#2b2440;color:#fff;font:600 22px system-ui,sans-serif;padding:10px 20px;border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,.25);white-space:nowrap';
      document.body.appendChild(el); }
    el.textContent = t; }, text);
  await p.goto('http://localhost:5173');
  await p.getByRole('button', { name: 'Live', exact: true }).click({ timeout: 15000 });
  await p.waitForTimeout(500); await cap('1. Type a goal: the mock planner turns it into 4 tasks'); mark('live');
  await p.getByPlaceholder('e.g. Plan a weekend in Lisbon').fill('Plan a weekend in Lisbon');
  await p.waitForTimeout(600);
  await p.getByRole('button', { name: 'Plan it' }).click(); mark('submitted');
  // wait for the first task to be picked up, then kill that worker
  let victim = null, jid = null;
  // a task job runs about 3 s; the planner job is instant, so require the same job on two polls 400 ms apart
  for (let i = 0; i < 100 && !victim; i++) {
    const a = await state(); await p.waitForTimeout(400); const b2 = await state();
    const w = b2.workers.find((w) => w.job_id && a.workers.some((x) => x.id === w.id && x.job_id === w.job_id));
    if (w) { victim = w.id; jid = w.job_id; }
  }
  await cap('2. Workers pick up tasks through Redis Streams'); await p.waitForTimeout(300);
  await p.locator('section[aria-labelledby="chaos-h"] select').selectOption(victim);
  await p.getByRole('button', { name: 'Kill worker' }).click(); mark('killed ' + victim); await cap('3. Kill ' + victim + ' mid-job: its message stays unacknowledged');
  // wait until the job is reclaimed by another worker
  for (let i = 0; i < 200; i++) { const s = await state(); if (s.workers.some((w) => w.id !== victim && w.job_id === jid)) break; await p.waitForTimeout(100); }
  mark('recovered'); await cap('4. Lease expires: another worker reclaims the job (XAUTOCLAIM)');
  await p.waitForTimeout(1800);
  await p.locator('section[aria-labelledby="chaos-h"] select').selectOption('');
  await p.getByRole('button', { name: 'Start worker' }).click(); mark('started'); await cap('5. Start a fresh worker');
  const approve = p.locator('.approvals button.primary').first();
  await approve.waitFor({ timeout: 20000 }); await cap('6. The last task waits for a human to approve it'); await p.waitForTimeout(1100);
  await approve.click(); mark('approved'); await cap('7. Approved: the last task runs');
  for (let i = 0; i < 100; i++) { const s = await state(); if (s.tasks.length && s.tasks.every((t) => t.status === 'succeeded')) break; await p.waitForTimeout(100); }
  await p.waitForTimeout(400); await cap('Goal done: one stored result per task, none duplicated');
  await p.waitForTimeout(2200); mark('end');
  await cdp.send('Page.stopScreencast');
  const list = ['ffconcat version 1.0'];
  frames.forEach((f, i) => {
    const next = frames[i + 1]?.t ?? f.t + 0.1;
    list.push(`file ${f.file}`, `duration ${(next - f.t).toFixed(3)}`);
  });
  list.push(`file ${frames.at(-1).file}`); // ffconcat needs the last file twice for its duration
  writeFileSync(`${out}/frames.txt`, list.join('\n') + '\n');
  console.log(`${frames.length} frames in ${out}/frames.txt`);
  await ctx.close(); await b.close();
})();
