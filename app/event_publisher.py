"""Event outbox publisher: copies committed rows of `events` into the Redis stream "events".

Events are inserted in the same transaction as the state change they describe, so a change can
never exist without its event (or the other way round). This publisher then moves them to Redis
the same way the job dispatcher does: XADD first, mark published in the same transaction after.
A crash between the two republishes the batch, so the stream is at-least-once; every event carries
its Postgres id, and consumers drop ids they have already seen. Nothing is ever lost.

Ordering: one publisher at a time (a transaction-level advisory lock) publishes in id order, so
stream order follows id order, except that two transactions that commit in the opposite order of
their ids can have their events appear in commit order instead. Consumers must not assume ids
only ever increase.
"""

import asyncio
import contextlib
import json
import logging

import asyncpg
from redis.asyncio import Redis

EVENTS_STREAM = "events"
STREAM_MAXLEN = 10_000  # approximate trim: the stream is a recent-history buffer, not the log
PUBLISHER_LOCK = 7_300_001  # arbitrary constant key for pg_try_advisory_xact_lock

log = logging.getLogger("event_publisher")


async def publish_events(pool: asyncpg.Pool, redis: Redis, batch_size: int = 500) -> int:
    """Publish up to batch_size unpublished events. Returns how many were published."""
    async with pool.acquire() as conn, conn.transaction():
        if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", PUBLISHER_LOCK):
            return 0  # another publisher is running; it keeps the order
        rows = await conn.fetch(
            """
            SELECT id, type, payload, created_at FROM events
            WHERE published_at IS NULL ORDER BY id LIMIT $1 FOR UPDATE
            """,
            batch_size,
        )
        if not rows:
            return 0
        for r in rows:
            await redis.xadd(
                EVENTS_STREAM,
                {
                    "id": str(r["id"]),
                    "type": r["type"],
                    "payload": r["payload"],
                    "ts": r["created_at"].isoformat(),
                },
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
        await conn.execute(
            "UPDATE events SET published_at = now() WHERE id = ANY($1::bigint[])",
            [r["id"] for r in rows],
        )
        return len(rows)


def parse_event(stream_id: str, fields: dict) -> dict:
    """Stream entry -> the JSON shape clients receive."""
    return {
        "id": int(fields["id"]),
        "stream_id": stream_id,
        "type": fields["type"],
        "payload": json.loads(fields["payload"]),
        "ts": fields["ts"],
    }


async def run_event_publisher(
    pool: asyncpg.Pool, redis: Redis, stop: asyncio.Event, interval: float = 0.05
) -> None:
    while not stop.is_set():
        try:
            published = await publish_events(pool, redis)
        except Exception:
            log.exception("event publisher cycle failed; retrying")
            published = 0
        if published == 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
