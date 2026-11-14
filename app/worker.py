"""Worker pool: N asyncio tasks reading the jobs stream through one Redis consumer group.

Delivery is at-least-once. A job can arrive twice (dispatcher republish, redelivery after a
crash), so every step is guarded in Postgres and the message is XACKed only AFTER the database
outcome is committed. See DECISIONS.md #1 and #5.

Cancellation rule: nothing here catches CancelledError and no XACK sits in a `finally`. A killed
worker therefore leaves its message unacknowledged (pending) for recovery on Day 6.
"""

import asyncio
import json
import logging
from uuid import UUID

import asyncpg
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.config import settings
from app.dispatcher import STREAM
from app.providers.mock_llm import MockLLM, ProviderError

GROUP = "workers"

log = logging.getLogger("worker")


async def ensure_group(redis: Redis) -> None:
    """Create the consumer group (and the stream if missing). Safe to call repeatedly."""
    try:
        # id="0": a group created after jobs were published still receives them.
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def _event(conn: asyncpg.Connection, type_: str, payload: dict) -> None:
    await conn.execute(
        "INSERT INTO events (type, payload) VALUES ($1, $2::jsonb)", type_, json.dumps(payload)
    )


async def _claim(pool: asyncpg.Pool, job_id: UUID, worker_id: str) -> bool:
    """Mark the job running for this worker. False if it already succeeded (or does not exist)."""
    async with pool.acquire() as conn, conn.transaction():
        claimed = await conn.fetchval(
            """
            UPDATE jobs
            SET status = 'running', locked_by = $2, started_at = now(), updated_at = now(),
                attempts = attempts + 1
            WHERE id = $1 AND status != 'succeeded'
            RETURNING id
            """,
            job_id,
            worker_id,
        )
        if claimed is None:
            return False
        await _event(conn, "job.started", {"job_id": str(job_id), "worker": worker_id})
        return True


async def _complete(pool: asyncpg.Pool, job_id: UUID, worker_id: str, output: dict) -> None:
    """Record success in ONE transaction. If another delivery already won, write nothing."""
    async with pool.acquire() as conn, conn.transaction():
        updated = await conn.fetchval(
            """
            UPDATE jobs SET status = 'succeeded', completed_at = now(), updated_at = now()
            WHERE id = $1 AND status != 'succeeded'
            RETURNING id
            """,
            job_id,
        )
        if updated is None:
            return
        await conn.execute(
            "INSERT INTO job_outputs (job_id, output) VALUES ($1, $2::jsonb)",
            job_id,
            json.dumps(output),
        )
        await _event(conn, "job.succeeded", {"job_id": str(job_id), "worker": worker_id})


async def _fail(pool: asyncpg.Pool, job_id: UUID, worker_id: str, error: str) -> None:
    """Day 4 behaviour: any provider error is final. Day 5 replaces this with retries + DLQ."""
    async with pool.acquire() as conn, conn.transaction():
        updated = await conn.fetchval(
            """
            UPDATE jobs SET status = 'failed', completed_at = now(), updated_at = now()
            WHERE id = $1 AND status != 'succeeded'
            RETURNING id
            """,
            job_id,
        )
        if updated is not None:
            await _event(
                conn, "job.failed", {"job_id": str(job_id), "worker": worker_id, "error": error}
            )


async def handle_message(
    pool: asyncpg.Pool,
    redis: Redis,
    provider: MockLLM,
    worker_id: str,
    msg_id: str,
    fields: dict,
) -> None:
    job_id = UUID(fields["job_id"])

    status = await pool.fetchval("SELECT status FROM jobs WHERE id = $1", job_id)
    if status is None or status == "succeeded":
        await redis.xack(STREAM, GROUP, msg_id)
        return

    if not await _claim(pool, job_id, worker_id):
        await redis.xack(STREAM, GROUP, msg_id)
        return

    payload = json.loads(fields.get("payload") or "{}")
    try:
        output = await provider.complete(
            payload.get("prompt", ""),
            mode=payload.get("mock_mode"),
            latency=payload.get("mock_latency"),
        )
    except (TimeoutError, ProviderError) as e:
        # Only these two are caught. CancelledError is a BaseException and passes straight
        # through, so a killed worker never reaches the XACK below.
        await _fail(pool, job_id, worker_id, str(e))
        await redis.xack(STREAM, GROUP, msg_id)
        return

    await _complete(pool, job_id, worker_id, output)
    # Commit happened above; only now acknowledge. A crash right here means redelivery, which
    # the guarded UPDATE turns into a no-op.
    await redis.xack(STREAM, GROUP, msg_id)


async def worker_loop(
    worker_id: str,
    pool: asyncpg.Pool,
    redis: Redis,
    provider: MockLLM,
    stop: asyncio.Event,
    block_ms: int = 500,
) -> None:
    while not stop.is_set():
        try:
            resp = await redis.xreadgroup(GROUP, worker_id, {STREAM: ">"}, count=1, block=block_ms)
            for _stream, entries in resp or []:
                for msg_id, fields in entries:
                    await handle_message(pool, redis, provider, worker_id, msg_id, fields)
        except Exception:
            # Unexpected error: the message stays pending (no XACK) and is recovered on Day 6.
            log.exception("%s: cycle failed; continuing", worker_id)
            await asyncio.sleep(0.5)


class WorkerPool:
    def __init__(
        self,
        pool: asyncpg.Pool,
        redis: Redis,
        provider: MockLLM,
        size: int = 4,
        block_ms: int = 500,
    ):
        self.pool, self.redis, self.provider = pool, redis, provider
        self.size, self.block_ms = size, block_ms
        self.tasks: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await ensure_group(self.redis)
        for i in range(1, self.size + 1):
            wid = f"worker-{i}"
            self.tasks[wid] = asyncio.create_task(
                worker_loop(wid, self.pool, self.redis, self.provider, self._stop, self.block_ms),
                name=wid,
            )

    async def kill_worker(self, worker_id: str) -> None:
        """Simulate a crash: cancel the task mid-flight. Its message stays unacknowledged."""
        task = self.tasks.pop(worker_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def stop(self) -> None:
        """Graceful shutdown: workers finish their current message, then exit."""
        self._stop.set()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()


async def main() -> None:
    import asyncpg
    from redis.asyncio import from_url

    pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=settings.worker_count + 2
    )
    redis = from_url(settings.redis_url, decode_responses=True)
    workers = WorkerPool(pool, redis, MockLLM(), size=settings.worker_count)
    await workers.start()
    try:
        await asyncio.Event().wait()
    finally:
        await workers.stop()
        await redis.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
