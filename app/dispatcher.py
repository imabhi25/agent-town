"""Outbox dispatcher: moves committed outbox rows into the Redis Stream.

Guarantee: a job is never lost between Postgres and Redis. Rows are marked published in the
same transaction that follows the XADD. If the process crashes after XADD but before commit,
the row stays unpublished and is published again later. That duplicate is safe because job
completion is idempotent (see DECISIONS.md #1).
"""

import asyncio
import contextlib
import logging

import asyncpg
from redis.asyncio import Redis

STREAM = "jobs"

log = logging.getLogger("dispatcher")


async def publish_pending(pool: asyncpg.Pool, redis: Redis, batch_size: int = 100) -> int:
    """Publish up to batch_size unpublished outbox rows. Returns how many were published."""
    async with pool.acquire() as conn, conn.transaction():
        # SKIP LOCKED lets several dispatchers run without publishing the same row twice.
        rows = await conn.fetch(
            """
            SELECT o.id AS outbox_id, j.id AS job_id, j.type, j.payload
            FROM outbox o
            JOIN jobs j ON j.id = o.job_id
            WHERE o.published_at IS NULL
            ORDER BY o.id
            LIMIT $1
            FOR UPDATE OF o SKIP LOCKED
            """,
            batch_size,
        )
        if not rows:
            return 0

        for r in rows:
            await redis.xadd(
                STREAM,
                {"job_id": str(r["job_id"]), "type": r["type"], "payload": r["payload"]},
            )

        await conn.execute(
            "UPDATE outbox SET published_at = now() WHERE id = ANY($1::bigint[])",
            [r["outbox_id"] for r in rows],
        )
        return len(rows)


async def run_dispatcher(
    pool: asyncpg.Pool,
    redis: Redis,
    stop: asyncio.Event,
    interval: float = 0.2,
    gate=None,
) -> None:
    """Poll the outbox until `stop` is set. Errors are logged and retried, never fatal.
    With a sleep gate (live demo) it does not poll while asleep."""
    while not stop.is_set():
        if gate is not None:
            await gate.wait_awake(stop)
            if stop.is_set():
                break
        try:
            published = await publish_pending(pool, redis)
        except Exception:
            log.exception("dispatcher cycle failed; retrying")
            published = 0
        if published == 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
