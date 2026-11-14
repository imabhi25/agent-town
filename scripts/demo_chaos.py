"""Run the chaos scenario against the docker compose Postgres and Redis and print a summary.

    python scripts/demo_chaos.py --up          # start compose services first
    python scripts/demo_chaos.py --jobs 500 --failure-rate 0.1

It is the same scenario as tests/test_chaos.py (app/chaos.py): jobs are submitted, workers are
killed and replaced while they run, and the script checks that nothing was lost or duplicated.
Workers run inside this process (killing one cancels its task, like a crash), against the real
Postgres and Redis from docker-compose.yml.
"""

import argparse
import asyncio
import subprocess
import sys

import asyncpg
from redis.asyncio import from_url

from app.chaos import ChaosResult, run_chaos
from app.config import settings
from app.dispatcher import STREAM
from app.retry import DELAYED_KEY, DLQ_STREAM

TABLES = "TRUNCATE goals, tasks, jobs, job_outputs, outbox, events CASCADE"


def summary(r: ChaosResult) -> str:
    ok = (
        r.succeeded == r.jobs
        and r.dead == 0
        and r.one_output_per_job
        and r.one_event_per_job
        and r.pending == 0
    )
    lines = [
        f"jobs submitted      {r.jobs}",
        f"jobs succeeded      {r.succeeded}",
        f"jobs dead (DLQ)     {r.dead}",
        f"workers killed      {r.kills}",
        f"recoveries          {r.recoveries}   (dead worker's job taken over)",
        f"retries             {r.retries}   (provider errors, backoff)",
        f"extra attempts      {r.extra_attempts}",
        f"job_outputs rows    {r.outputs}   (one per job: {r.one_output_per_job})",
        f"job.succeeded     {len(r.succeeded_events)} jobs   (exactly one each: {r.one_event_per_job})",
        f"XPENDING            {r.pending}",
        f"duration            {r.duration:.1f}s",
        "",
        "RESULT: PASS (nothing lost, nothing duplicated)" if ok else "RESULT: FAIL",
    ]
    return "\n".join(lines)


async def main(args: argparse.Namespace) -> bool:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    redis = from_url(settings.redis_url, decode_responses=True)
    try:
        if await pool.fetchval("SELECT count(*) FROM jobs") and not args.reset:
            print("The jobs table is not empty. Re-run with --reset to wipe it first.")
            return False
        if args.reset:
            await pool.execute(TABLES)
            await redis.delete(STREAM, DLQ_STREAM, DELAYED_KEY)
        result = await run_chaos(
            pool,
            redis,
            jobs=args.jobs,
            workers=args.workers,
            failure_rate=args.failure_rate,
            seed=args.seed,
        )
    finally:
        await redis.aclose()
        await pool.close()
    print(summary(result))
    return result.succeeded == result.jobs and result.pending == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=200)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--failure-rate", type=float, default=0.1, help="mock 429/500 rate")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--up", action="store_true", help="docker compose up postgres + redis")
    parser.add_argument(
        "--reset", action="store_true", help="TRUNCATE tables and delete the job streams first"
    )
    a = parser.parse_args()
    if a.up:
        subprocess.run(["docker", "compose", "up", "-d", "--wait", "postgres", "redis"], check=True)
    sys.exit(0 if asyncio.run(main(a)) else 1)
