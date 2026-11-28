"""One chaos-campaign process: a single worker, or the outbox dispatcher.

    python -m app.chaos_proc worker worker-7 [--unsafe] [--failure-rate 0.05]
    python -m app.chaos_proc dispatcher

scripts/bench_engine.py uses the same processes with --latency and --production-clocks.

scripts/chaos_campaign.py starts many of these and kills them with SIGKILL (kill -9), so a
"crash" here is a real dead process: no finally blocks, no graceful shutdown, open Postgres
transactions rolled back by the server, the heartbeat key left to expire. That is the difference
from app/chaos.py, where a killed worker is a cancelled asyncio task in the test's own process.

The engine code is the production code (app/worker.py, app/dispatcher.py). Only the clocks are
short (app.chaos.CHAOS_RECOVERY) and the provider is the mock, so a run takes seconds.
"""

import argparse
import asyncio
import random

from redis.asyncio import from_url
from redis.exceptions import RedisError

from app.chaos import CHAOS_RECOVERY
from app.config import settings
from app.db import create_pool
from app.dispatcher import run_dispatcher
from app.providers.mock_llm import MockLLM
from app.recovery import RecoveryConfig
from app.retry import RetryPolicy
from app.worker import WorkerPool


def _redis():
    # Short connect timeout: while Redis is down, fail fast and retry instead of hanging.
    return from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=1.0)


async def _until_ok(step) -> None:
    while True:
        try:
            return await step()
        except (RedisError, OSError):
            await asyncio.sleep(0.1 + random.random() * 0.1)


async def run_worker(
    worker_id: str,
    unsafe: bool,
    failure_rate: float,
    latency: tuple[float, float] = (0.02, 0.08),
    recovery: RecoveryConfig = CHAOS_RECOVERY,
) -> None:
    pool = await create_pool(min_size=1, max_size=2)
    redis = _redis()
    provider = MockLLM(failure_rate=failure_rate, min_latency=latency[0], max_latency=latency[1])
    workers = WorkerPool(
        pool,
        redis,
        provider,
        size=0,  # no default ids; this process runs exactly one worker, named by the campaign
        block_ms=50,
        policy=RetryPolicy(base=0.05, cap=0.5),
        mover_interval=0.05,  # every worker process also moves due retries; duplicates are safe
        recovery=recovery,
    )
    workers.extras.unsafe_ack_before_commit = unsafe
    # Redis may be down at start (a restart in progress): keep trying each step until it works.
    await _until_ok(workers.start)
    await _until_ok(lambda: workers.start_worker(worker_id))
    await asyncio.Event().wait()  # runs until killed


async def run_dispatch() -> None:
    pool = await create_pool(min_size=1, max_size=2)
    await run_dispatcher(pool, _redis(), asyncio.Event(), interval=0.05)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("role", choices=["worker", "dispatcher"])
    p.add_argument("worker_id", nargs="?", default="worker-1")
    p.add_argument("--unsafe", action="store_true", help="XACK before commit (deliberate bug)")
    p.add_argument("--failure-rate", type=float, default=0.0)
    p.add_argument("--latency", type=float, nargs=2, default=(0.02, 0.08), metavar=("MIN", "MAX"))
    p.add_argument(
        "--production-clocks",
        action="store_true",
        help="use the configured lease/heartbeat (settings) instead of the short chaos clocks",
    )
    a = p.parse_args()
    if a.role == "worker":
        recovery = RecoveryConfig() if a.production_clocks else CHAOS_RECOVERY
        asyncio.run(
            run_worker(a.worker_id, a.unsafe, a.failure_rate, tuple(a.latency), recovery)
        )
    else:
        asyncio.run(run_dispatch())


if __name__ == "__main__":
    main()
