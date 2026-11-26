"""Dead-letter queue operations."""

from uuid import UUID

import asyncpg
from redis.asyncio import Redis

from app.events import add_event
from app.retry import DLQ_STREAM


async def replay_dlq(pool: asyncpg.Pool, redis: Redis, job_id: UUID | str) -> bool:
    """Give a dead job a fresh retry budget and put it back in the queue.

    Returns False if the job is not 'dead' (nothing to replay). The reset, the outbox row and the
    job.replayed event commit together; the dispatcher then publishes the outbox row to the
    stream. Going through the outbox (not a direct XADD) means a crash right after the commit
    cannot lose the job.
    """
    job_id = UUID(str(job_id))
    async with pool.acquire() as conn, conn.transaction():
        reset = await conn.fetchval(
            """
            UPDATE jobs
            SET status = 'queued', attempts = 0, locked_by = NULL, lock_token = NULL,
                started_at = NULL, completed_at = NULL, updated_at = now()
            WHERE id = $1 AND status = 'dead'
            RETURNING id
            """,
            job_id,
        )
        if reset is None:
            return False
        await conn.execute("INSERT INTO outbox (job_id) VALUES ($1)", job_id)
        await add_event(conn, "job.replayed", {"job_id": str(job_id)})

    # Tidy the DLQ view. Best effort: the job's status in Postgres is already authoritative.
    for entry_id, fields in await redis.xrange(DLQ_STREAM):
        if fields.get("job_id") == str(job_id):
            await redis.xdel(DLQ_STREAM, entry_id)
    return True
