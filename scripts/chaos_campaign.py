"""Chaos campaign: many runs of real crashes, each checked against the delivery guarantee.

    python scripts/chaos_campaign.py --reset                       # the full campaign
    python scripts/chaos_campaign.py --reset --runs 2 --jobs 150 --unsafe-runs 1   # CI size
    python scripts/chaos_report.py                                  # README numbers from results

Each run:
  1. starts a private redis-server (AOF on, fsync always) on --redis-port, in a fresh directory
  2. submits --jobs mock jobs through the outbox (as POST /jobs does)
  3. starts --workers worker PROCESSES and one dispatcher process (app/chaos_proc.py)
  4. injects faults until every job is finished (or progress stops): kill -9 of a worker (mostly
     one that is mid-job), kill -9 of the dispatcher, kill -9 of Redis followed by a restart
  5. stops injecting, lets the survivors drain, kills everything
  6. runs app/checker.py on Postgres and appends one JSON line to --out

Safe runs use the real worker. Unsafe runs (--unsafe-runs) use the same worker with a deliberate
bug, XACK before the completion commit, to show the checker catches what the real ordering
prevents. The script exits non-zero if a safe run has a violation or an unsafe run has none.

Needs Postgres (DATABASE_URL, schema loaded) and a `redis-server` binary on PATH. It never
touches the Redis in REDIS_URL: workers are pointed at the private one.
"""

import argparse
import asyncio
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
from redis.asyncio import from_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chaos import submit_jobs
from app.checker import KINDS, check
from app.db import create_pool
from app.dispatcher import STREAM
from app.worker import GROUP

TABLES = "TRUNCATE goals, tasks, jobs, job_outputs, outbox, events CASCADE"
ROOT = Path(__file__).resolve().parent.parent


class RedisServer:
    """A redis-server we own, so we can kill -9 it and start it again on the same data."""

    def __init__(self, port: int):
        self.port = port
        self.dir = tempfile.mkdtemp(prefix="chaos-redis-")
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [
                "redis-server",
                "--port", str(self.port),
                "--dir", self.dir,
                "--save", "",
                # appendfsync always: every write acknowledged to a client is on disk, so a kill -9
                # loses no acknowledged write. The campaign tests crashes, not Redis data loss.
                "--appendonly", "yes",
                "--appendfsync", "always",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def wait_ready(self, timeout: float = 10.0) -> None:
        r = from_url(f"redis://localhost:{self.port}/0")
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    await r.ping()
                    return
                except Exception:
                    if time.monotonic() > deadline:
                        raise
                    await asyncio.sleep(0.05)
        finally:
            await r.aclose()

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()

    def cleanup(self) -> None:
        self.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


@dataclass
class Faults:
    worker_kills: int = 0
    worker_kills_mid_job: int = 0
    dispatcher_kills: int = 0
    redis_restarts: int = 0


@dataclass
class RunResult:
    campaign: str
    run: int
    mode: str  # "safe" | "unsafe"
    seed: int
    jobs: int
    workers: int
    faults: Faults
    succeeded: int = 0
    dead: int = 0
    starts: int = 0
    redelivered_jobs: int = 0
    recoveries: int = 0
    retries: int = 0
    violations: dict[str, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)  # a few violations, for the reader
    pending_after: int = -1
    duration_s: float = 0.0

    @property
    def total_violations(self) -> int:
        return sum(self.violations.values())


class Procs:
    """The worker and dispatcher processes of one run."""

    def __init__(self, env: dict, unsafe: bool, failure_rate: float):
        self.env, self.unsafe, self.failure_rate = env, unsafe, failure_rate
        self.workers: dict[str, subprocess.Popen] = {}
        self.dispatcher: subprocess.Popen | None = None
        self._next = 1

    def _spawn(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "app.chaos_proc", *args],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,  # worker logs show expected errors while Redis is down
        )

    def start_worker(self) -> str:
        wid = f"w{self._next}"
        self._next += 1
        args = ["worker", wid, "--failure-rate", str(self.failure_rate)]
        if self.unsafe:
            args.append("--unsafe")
        self.workers[wid] = self._spawn(*args)
        return wid

    def start_dispatcher(self) -> None:
        self.dispatcher = self._spawn("dispatcher")

    def kill_worker(self, wid: str) -> None:
        p = self.workers.pop(wid)
        p.send_signal(signal.SIGKILL)  # kill -9: no cleanup of any kind runs
        p.wait()

    def kill_dispatcher(self) -> None:
        if self.dispatcher:
            self.dispatcher.send_signal(signal.SIGKILL)
            self.dispatcher.wait()

    def kill_all(self) -> None:
        for wid in list(self.workers):
            self.kill_worker(wid)
        self.kill_dispatcher()


async def run_once(
    pool: asyncpg.Pool,
    args: argparse.Namespace,
    campaign: str,
    run: int,
    unsafe: bool,
    seed: int,
) -> RunResult:
    rng = random.Random(seed)
    await pool.execute(TABLES)
    redis_srv = RedisServer(args.redis_port)
    redis_srv.start()
    await redis_srv.wait_ready()
    redis_url = f"redis://localhost:{args.redis_port}/0"
    env = {**os.environ, "REDIS_URL": redis_url, "PYTHONPATH": str(ROOT)}
    procs = Procs(env, unsafe, args.failure_rate)
    faults = Faults()
    started = time.monotonic()

    # max_attempts=10 (the API maximum): a takeover counts as an attempt, and repeated kills of
    # one job must not push a healthy job into the DLQ.
    job_ids = await submit_jobs(pool, args.jobs, f"{campaign}-{run}", max_attempts=10)

    async def unfinished() -> int:
        return await pool.fetchval(
            "SELECT count(*) FROM jobs WHERE id = ANY($1) AND status NOT IN ('succeeded','dead')",
            job_ids,
        )

    procs.start_dispatcher()
    for _ in range(args.workers):
        procs.start_worker()

    try:
        # Fault phase. Ends when every job is finished, when nothing has finished for
        # --stall seconds (an unsafe run that lost jobs never finishes), or at --max-seconds.
        last_left, last_progress = await unfinished(), time.monotonic()
        while time.monotonic() - started < args.max_seconds:
            left = await unfinished()
            if left == 0:
                break
            if left < last_left:
                last_left, last_progress = left, time.monotonic()
            elif time.monotonic() - last_progress > args.stall:
                break
            await asyncio.sleep(rng.uniform(*args.kill_every))

            roll = rng.random()
            if roll < args.p_redis:
                redis_srv.kill()  # kill -9 Redis; every client connection drops
                await asyncio.sleep(rng.uniform(0.2, 1.0))
                redis_srv.start()
                await redis_srv.wait_ready()
                faults.redis_restarts += 1
            elif roll < args.p_redis + args.p_dispatcher:
                procs.kill_dispatcher()
                procs.start_dispatcher()
                faults.dispatcher_kills += 1
            else:
                # Prefer a worker that is mid-job, so the crash lands inside the window that
                # matters (claimed, provider call in flight, not yet committed).
                busy = {
                    r["locked_by"]
                    for r in await pool.fetch(
                        "SELECT DISTINCT locked_by FROM jobs WHERE status = 'running'"
                    )
                } & procs.workers.keys()
                mid_job = bool(busy) and rng.random() < 0.8
                victim = rng.choice(sorted(busy if mid_job else procs.workers))
                procs.kill_worker(victim)
                procs.start_worker()
                faults.worker_kills += 1
                faults.worker_kills_mid_job += mid_job

        # Drain phase: no more faults. The survivors get --drain seconds to finish and to
        # acknowledge what is left pending (leases of the killed workers expire meanwhile).
        redis = from_url(redis_url, decode_responses=True)
        drain_end = time.monotonic() + args.drain
        pending = -1
        while time.monotonic() < drain_end:
            pending = (await redis.xpending(STREAM, GROUP))["pending"]
            if await unfinished() == 0 and pending == 0:
                break
            await asyncio.sleep(0.2)
        await redis.aclose()
    finally:
        procs.kill_all()
        redis_srv.cleanup()

    report = await check(pool, job_ids)
    return RunResult(
        campaign=campaign,
        run=run,
        mode="unsafe" if unsafe else "safe",
        seed=seed,
        jobs=report.jobs,
        workers=args.workers,
        faults=faults,
        succeeded=report.succeeded,
        dead=report.dead,
        starts=report.starts,
        redelivered_jobs=report.redelivered_jobs,
        recoveries=report.recoveries,
        retries=report.retries,
        violations={k: report.count(k) for k in KINDS},
        examples=[f"{v.kind}: {v.job_id} {v.detail}" for v in report.violations[:3]],
        pending_after=pending,
        duration_s=round(time.monotonic() - started, 2),
    )


def line(r: RunResult) -> str:
    f = r.faults
    verdict = "OK" if r.total_violations == 0 else f"{r.total_violations} VIOLATIONS"
    return (
        f"run {r.run:>2} {r.mode:<6} jobs={r.jobs} succeeded={r.succeeded} dead={r.dead} "
        f"kills={f.worker_kills} (mid-job {f.worker_kills_mid_job}) "
        f"dispatcher={f.dispatcher_kills} "
        f"redis={f.redis_restarts} redelivered={r.redelivered_jobs} "
        f"{r.duration_s:.1f}s -> {verdict}"
    )


def _append(path: Path, text: str) -> None:
    """One line per run, written as it finishes, so an interrupted campaign keeps its runs."""
    with path.open("a") as fh:
        fh.write(text + "\n")


async def main(args: argparse.Namespace) -> int:
    if shutil.which("redis-server") is None:
        print("redis-server not found on PATH (apt install redis-server / brew install redis)")
        return 2
    pool = await create_pool(min_size=1, max_size=5)
    try:
        if await pool.fetchval("SELECT count(*) FROM jobs") and not args.reset:
            print("The jobs table is not empty. Re-run with --reset to wipe it first.")
            return 2
        campaign = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base_seed = args.seed if args.seed is not None else random.randrange(1 << 30)
        plan = [False] * args.runs + [True] * args.unsafe_runs
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("")
        results = []
        for i, unsafe in enumerate(plan, start=1):
            r = await run_once(pool, args, campaign, i, unsafe, base_seed + i)
            results.append(r)
            _append(out, json.dumps({**asdict(r), "total_violations": r.total_violations}))
            print(line(r), flush=True)
            for ex in r.examples:
                print("    " + ex)
        await pool.execute(TABLES)
    finally:
        await pool.close()

    safe_bad = [r for r in results if r.mode == "safe" and r.total_violations]
    unsafe_missed = [r for r in results if r.mode == "unsafe" and not r.total_violations]
    print(f"\nresults written to {args.out}")
    if safe_bad:
        print(f"FAIL: {len(safe_bad)} safe run(s) violated the guarantee")
    if unsafe_missed:
        print(f"FAIL: the checker missed the bug in {len(unsafe_missed)} unsafe run(s)")
    return 1 if safe_bad or unsafe_missed else 0


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs", type=int, default=20, help="safe runs")
    p.add_argument("--unsafe-runs", type=int, default=10, help="runs with XACK before commit")
    p.add_argument("--jobs", type=int, default=500, help="jobs per run")
    p.add_argument("--workers", type=int, default=6, help="worker processes per run")
    p.add_argument("--failure-rate", type=float, default=0.05, help="mock 429/500 rate")
    p.add_argument("--kill-every", type=float, nargs=2, default=(0.1, 0.35), metavar=("MIN", "MAX"))
    p.add_argument("--p-redis", type=float, default=0.04, help="chance a fault is a Redis restart")
    p.add_argument("--p-dispatcher", type=float, default=0.06, help="chance it is the dispatcher")
    p.add_argument(
        "--stall", type=float, default=4.0, help="end faults after this long with no progress"
    )
    p.add_argument("--max-seconds", type=float, default=120.0, help="cap on the fault phase")
    p.add_argument("--drain", type=float, default=8.0, help="quiet time for survivors to finish")
    p.add_argument("--redis-port", type=int, default=6390)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--out", default="results/chaos/campaign.jsonl")
    p.add_argument("--reset", action="store_true", help="TRUNCATE the job tables first")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse())))
