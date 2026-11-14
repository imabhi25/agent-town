"""Synthetic load generator: keeps a local system lightly busy so the map is never empty.

Mock jobs only (type llm.mock), a few per tick, with short random latency. Off by default and
forced off in the live demo, where idle must really be idle (see the sleep gate).
"""

import asyncio
import contextlib
import logging
import random
import uuid

import asyncpg

from app.jobs import create_job

log = logging.getLogger("loadgen")


async def submit_mock_jobs(
    pool: asyncpg.Pool, count: int, prefix: str, latency: float | None = None, max_attempts: int = 5
) -> int:
    """Create `count` mock jobs (and their outbox rows) in one transaction."""
    batch = uuid.uuid4().hex[:10]
    async with pool.acquire() as conn, conn.transaction():
        for i in range(count):
            payload = {"prompt": f"{prefix} job {i}"}
            if latency is not None:
                payload["mock_latency"] = latency
            await create_job(
                conn, "llm.mock", payload, f"{prefix}:{batch}:{i}", max_attempts, trusted=True
            )
    return count


async def run_load_generator(
    pool: asyncpg.Pool,
    stop: asyncio.Event,
    interval: float = 2.0,
    rng: random.Random | None = None,
) -> None:
    rng = rng or random.Random()
    while not stop.is_set():
        try:
            await submit_mock_jobs(
                pool, rng.randint(1, 3), "load", latency=round(rng.uniform(0.2, 1.5), 2)
            )
        except Exception:
            log.exception("load generator tick failed; retrying")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
