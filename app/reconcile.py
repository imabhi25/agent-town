"""Startup reconcile: Redis may lose data (free tiers evict or reset, a flush, a restore), but
Postgres is the source of truth. Any job that Postgres says is still in flight (queued, running,
retrying) but that is on neither the jobs stream nor the delayed set is put back through the
outbox, so the dispatcher publishes it again. Re-publishing a job that was in fact fine is safe
(completion is idempotent), so this errs on the side of re-enqueueing.

It also refills an empty events stream with the most recent events so new clients still get a
snapshot with history.
"""

import logging

import asyncpg
from redis.asyncio import Redis

from app.dispatcher import STREAM
from app.event_publisher import EVENTS_STREAM
from app.events import add_event
from app.retry import DELAYED_KEY

log = logging.getLogger("reconcile")

REPUBLISH_EVENTS = 200


async def _job_ids_in_redis(redis: Redis) -> set[str]:
    present: set[str] = set(await redis.zrange(DELAYED_KEY, 0, -1))
    start = "-"
    while True:
        entries = await redis.xrange(STREAM, min=start, max="+", count=1000)
        if not entries:
            break
        present.update(f["job_id"] for _, f in entries if "job_id" in f)
        if len(entries) < 1000:
            break
        start = "(" + entries[-1][0]
    return present


async def reconcile(pool: asyncpg.Pool, redis: Redis) -> dict:
    """Returns {"jobs": n re-enqueued, "events": n marked for republish}."""
    in_flight = await pool.fetch(
        "SELECT id FROM jobs WHERE status IN ('queued', 'running', 'retrying')"
    )
    requeued = 0
    if in_flight:
        present = await _job_ids_in_redis(redis)
        async with pool.acquire() as conn, conn.transaction():
            waiting = {
                str(r["job_id"])
                for r in await conn.fetch("SELECT job_id FROM outbox WHERE published_at IS NULL")
            }
            for r in in_flight:
                job_id = str(r["id"])
                if job_id in present or job_id in waiting:
                    continue
                await conn.execute("INSERT INTO outbox (job_id) VALUES ($1)", r["id"])
                await add_event(conn, "job.reconciled", {"job_id": job_id})
                requeued += 1

    republished = 0
    if await redis.xlen(EVENTS_STREAM) == 0:
        republished = int(
            (
                await pool.execute(
                    """
                    UPDATE events SET published_at = NULL
                    WHERE id IN (SELECT id FROM events ORDER BY id DESC LIMIT $1)
                    """,
                    REPUBLISH_EVENTS,
                )
            ).split()[-1]
        )
    if requeued or republished:
        log.warning("reconcile: re-enqueued %d jobs, republishing %d events", requeued, republished)
    return {"jobs": requeued, "events": republished}
