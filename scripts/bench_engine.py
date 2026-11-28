"""Engine benchmark: throughput and latency as worker processes scale, and recovery time after
kill -9. Writes one JSON line per measurement; scripts/bench_report.py turns them into tables.

    python scripts/bench_engine.py --reset                 # full run, about 10 minutes
    python scripts/bench_engine.py --reset --workers 1 2 --recovery-trials 2   # quick look

Workers are separate processes (app/chaos_proc.py), as in the chaos campaign, so this measures
the engine past the one-process ceiling of the older k6 benchmark (bench/). The LLM is the mock,
50 to 200 ms per call (mean 125 ms), the same as bench/, so one worker's ideal is 8 jobs/s.

Three measurements per worker count N:
  throughput  100*N jobs submitted at once; jobs/s from the first job started to the last done
  latency     a steady 60% of that throughput for --steady-seconds; created -> completed per job
              (outbox poll + queue wait + provider call + commit)
And recovery, with the production clocks (lease, heartbeat TTL, recovery interval from settings):
  a slow job is started, its worker is killed with SIGKILL, and we time kill -> job.recovered
  (another worker took it over) and kill -> succeeded.

Needs Postgres (DATABASE_URL, schema loaded) and `redis-server` on PATH (a private one is
started on --redis-port). Wipes the job tables.
"""

import argparse
import asyncio
import json
import math
import os
import platform
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import asyncpg
from redis.asyncio import from_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from chaos_campaign import ROOT, TABLES, Procs, RedisServer

from app.chaos import submit_jobs
from app.config import settings
from app.db import create_pool
from app.recovery import heartbeat_key

LATENCY = (0.05, 0.2)  # mock provider seconds per call, as in bench/
MEAN_LATENCY = sum(LATENCY) / 2


def _append(path: Path, row: dict) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile (no interpolation), so every reported value was observed."""
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)]


class Stack:
    """Private Redis + dispatcher + N worker processes for one measurement."""

    def __init__(self, port: int, workers: int, production_clocks: bool = False):
        self.redis_srv = RedisServer(port)
        self.url = f"redis://localhost:{port}/0"
        flags = ["--latency", str(LATENCY[0]), str(LATENCY[1])]
        if production_clocks:
            flags.append("--production-clocks")
        env = {**os.environ, "REDIS_URL": self.url, "PYTHONPATH": str(ROOT)}
        self.procs = Procs(env, unsafe=False, failure_rate=0.0, worker_args=tuple(flags))
        self.n = workers

    async def __aenter__(self) -> Self:
        self.redis_srv.start()
        await self.redis_srv.wait_ready()
        self.redis = from_url(self.url, decode_responses=True)
        self.procs.start_dispatcher()
        ids = [self.procs.start_worker() for _ in range(self.n)]
        # Ready = every worker has written its first heartbeat (it does so before reading).
        deadline = time.monotonic() + 30
        while not all([await self.redis.exists(heartbeat_key(w)) for w in ids]):
            if time.monotonic() > deadline:
                raise RuntimeError("workers did not start")
            await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.redis.aclose()
        self.procs.kill_all()
        self.redis_srv.cleanup()


async def _wait_done(pool: asyncpg.Pool, ids: list, timeout: float = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = await pool.fetchval(
            "SELECT count(*) FROM jobs WHERE id = ANY($1) AND status <> 'succeeded'", ids
        )
        if left == 0:
            return
        await asyncio.sleep(0.1)
    raise RuntimeError(f"{left} jobs not done after {timeout}s")


async def throughput(pool: asyncpg.Pool, args, n: int, run: str) -> dict:
    await pool.execute(TABLES)
    jobs = 100 * n
    async with Stack(args.redis_port, n):
        ids = await submit_jobs(pool, jobs, f"{run}-tp{n}", max_attempts=3)
        await _wait_done(pool, ids)
    span = await pool.fetchval(
        "SELECT extract(epoch FROM max(completed_at) - min(started_at)) FROM jobs WHERE id = ANY($1)",
        ids,
    )
    span = float(span)
    return {
        "kind": "throughput",
        "workers": n,
        "jobs": jobs,
        "seconds": round(span, 2),
        "jobs_per_s": round(jobs / span, 1),
        "ideal_jobs_per_s": round(n / MEAN_LATENCY, 1),
    }


async def latency(pool: asyncpg.Pool, args, n: int, run: str, capacity: float) -> dict:
    await pool.execute(TABLES)
    rate = round(0.6 * capacity, 1)
    tick = 0.05
    ids: list = []
    async with Stack(args.redis_port, n):
        started = time.monotonic()
        owed = 0.0
        batch = 0
        while time.monotonic() - started < args.steady_seconds:
            owed += rate * tick
            k = int(owed)
            if k:
                owed -= k
                # Each batch is its own transaction, so created_at is its real arrival time.
                ids += await submit_jobs(pool, k, f"{run}-lat{n}-{batch}", max_attempts=3)
                batch += 1
            await asyncio.sleep(tick)
        await _wait_done(pool, ids)
    rows = await pool.fetch(
        "SELECT extract(epoch FROM completed_at - created_at) * 1000 AS ms FROM jobs "
        "WHERE id = ANY($1)",
        ids,
    )
    ms = [float(r["ms"]) for r in rows]
    return {
        "kind": "latency",
        "workers": n,
        "offered_jobs_per_s": rate,
        "jobs": len(ms),
        "p50_ms": round(_pct(ms, 0.50)),
        "p95_ms": round(_pct(ms, 0.95)),
        "p99_ms": round(_pct(ms, 0.99)),
    }


async def recovery(pool: asyncpg.Pool, args, trial: int, run: str) -> dict:
    """Kill the worker running a 2 s job; time the takeover and the completion."""
    await pool.execute(TABLES)
    async with Stack(args.redis_port, 3, production_clocks=True) as stack:
        async with pool.acquire() as conn, conn.transaction():
            job_id = await conn.fetchval(
                "INSERT INTO jobs (type, payload, idempotency_key, max_attempts) "
                "VALUES ('llm.mock', $1::jsonb, $2, 5) RETURNING id",
                json.dumps({"prompt": "slow", "mock_latency": 2.0}),
                f"{run}-rec{trial}",
            )
            await conn.execute("INSERT INTO outbox (job_id) VALUES ($1)", job_id)
        owner, deadline = None, time.monotonic() + 30
        while owner is None:
            if time.monotonic() > deadline:
                raise RuntimeError("the slow job never started")
            owner = await pool.fetchval(
                "SELECT locked_by FROM jobs WHERE id = $1 AND status = 'running'", job_id
            )
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)  # well inside the 2 s call
        stack.procs.kill_worker(owner)
        killed_at = await pool.fetchval("SELECT clock_timestamp()")
        await _wait_done(pool, [job_id], timeout=120)
    reclaimed = await pool.fetchval(
        "SELECT min(created_at) FROM events WHERE type = 'job.recovered' "
        "AND payload->>'job_id' = $1",
        str(job_id),
    )
    done = await pool.fetchval("SELECT completed_at FROM jobs WHERE id = $1", job_id)
    return {
        "kind": "recovery",
        "trial": trial,
        "lease_s": settings.lease_seconds,
        "heartbeat_ttl_s": settings.heartbeat_ttl,
        "recovery_interval_s": settings.recovery_interval,
        "kill_to_reclaim_s": round((reclaimed - killed_at).total_seconds(), 2),
        "kill_to_done_s": round((done - killed_at).total_seconds(), 2),
    }


async def main(args: argparse.Namespace) -> int:
    if shutil.which("redis-server") is None:
        print("redis-server not found on PATH")
        return 2
    pool = await create_pool(min_size=1, max_size=5)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("")
    run = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        if await pool.fetchval("SELECT count(*) FROM jobs") and not args.reset:
            print("The jobs table is not empty. Re-run with --reset to wipe it first.")
            return 2
        meta = {
            "kind": "meta",
            "run": run,
            "cpu": _cpu_name(),
            "cores": os.cpu_count(),
            "python": platform.python_version(),
            "postgres": await pool.fetchval("SHOW server_version"),
            "mock_latency_s": list(LATENCY),
        }
        _append(out, meta)
        print(meta, flush=True)
        for n in args.workers:
            tp = await throughput(pool, args, n, run)
            _append(out, tp)
            print(tp, flush=True)
            lat = await latency(pool, args, n, run, tp["jobs_per_s"])
            _append(out, lat)
            print(lat, flush=True)
        for t in range(1, args.recovery_trials + 1):
            rec = await recovery(pool, args, t, run)
            _append(out, rec)
            print(rec, flush=True)
        await pool.execute(TABLES)
    finally:
        await pool.close()
    print(f"results written to {out}")
    return 0


def _cpu_name() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4, 8, 16])
    p.add_argument("--steady-seconds", type=float, default=15.0)
    p.add_argument("--recovery-trials", type=int, default=8)
    p.add_argument("--redis-port", type=int, default=6391)
    p.add_argument("--out", default="results/bench/engine.jsonl")
    p.add_argument("--reset", action="store_true", help="TRUNCATE the job tables first")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse())))
