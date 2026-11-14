"""Worker pool: N asyncio tasks reading the jobs stream through one Redis consumer group.

Delivery is at-least-once. A job can arrive twice (dispatcher republish, redelivery after a
crash), so every step is guarded in Postgres and the message is XACKed only AFTER the database
outcome is committed. See DECISIONS.md #1 and #5.

Cancellation rule: nothing here catches CancelledError and no XACK sits in a `finally`. A killed
worker therefore leaves its message unacknowledged (pending) for recovery on Day 6.
"""

import asyncio
import contextlib
import json
import logging
import time
from uuid import UUID

import asyncpg
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.config import settings
from app.dispatcher import STREAM
from app.events import add_event
from app.providers.mock_llm import MockLLM, ProviderError
from app.retry import DELAYED_KEY, DLQ_STREAM, RetryPolicy, is_retryable

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


async def _claim(pool: asyncpg.Pool, job_id: UUID, worker_id: str) -> asyncpg.Record | None:
    """Mark the job running for this worker. None if it already succeeded or died.

    Returns (attempts, max_attempts) so the caller knows whether a failure may be retried.
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            UPDATE jobs
            SET status = 'running', locked_by = $2, started_at = now(), updated_at = now(),
                attempts = attempts + 1
            WHERE id = $1 AND status NOT IN ('succeeded', 'dead')
            RETURNING attempts, max_attempts
            """,
            job_id,
            worker_id,
        )
        if row is None:
            return None
        await add_event(conn, "job.started", {"job_id": str(job_id), "worker": worker_id})
        return row


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
        await add_event(conn, "job.succeeded", {"job_id": str(job_id), "worker": worker_id})


async def _schedule_retry(
    pool: asyncpg.Pool, job_id: UUID, worker_id: str, attempt: int, delay: float, error: str
) -> bool:
    async with pool.acquire() as conn, conn.transaction():
        updated = await conn.fetchval(
            """
            UPDATE jobs SET status = 'retrying', updated_at = now()
            WHERE id = $1 AND status NOT IN ('succeeded', 'dead')
            RETURNING id
            """,
            job_id,
        )
        if updated is None:
            return False
        await add_event(
            conn,
            "job.retry_scheduled",
            {
                "job_id": str(job_id),
                "worker": worker_id,
                "attempt": attempt,
                "delay": round(delay, 3),
                "error": error,
            },
        )
        return True


async def _bury(
    pool: asyncpg.Pool,
    redis: Redis,
    job_id: UUID,
    worker_id: str,
    attempts: int,
    error: str,
    reason: str,
) -> None:
    """Mark the job dead and add it to the DLQ stream.

    The XADD sits inside the transaction, before COMMIT. If it fails, the transaction rolls back
    and the message stays pending (nothing is lost). If COMMIT fails after the XADD, the message
    is redelivered and the DLQ may get a duplicate entry, which is harmless: the DLQ is
    at-least-once and jobs.status = 'dead' is the source of truth.
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            UPDATE jobs SET status = 'dead', completed_at = now(), updated_at = now()
            WHERE id = $1 AND status NOT IN ('succeeded', 'dead')
            RETURNING type, payload
            """,
            job_id,
        )
        if row is None:
            return
        await add_event(
            conn,
            "job.dead",
            {
                "job_id": str(job_id),
                "worker": worker_id,
                "attempts": attempts,
                "error": error,
                "reason": reason,
            },
        )
        await redis.xadd(
            DLQ_STREAM,
            {
                "job_id": str(job_id),
                "type": row["type"],
                "payload": row["payload"],
                "error": error,
                "attempts": str(attempts),
                "reason": reason,
            },
        )


async def _handle_error(
    pool: asyncpg.Pool,
    redis: Redis,
    policy: RetryPolicy,
    job_id: UUID,
    worker_id: str,
    claim: asyncpg.Record,
    error: Exception,
) -> None:
    attempts = claim["attempts"]
    if is_retryable(error) and attempts < claim["max_attempts"]:
        delay = policy.delay(attempts)
        if await _schedule_retry(pool, job_id, worker_id, attempts, delay, str(error)):
            # The delay lives in Redis, not in this worker: the worker is free right away.
            await redis.zadd(DELAYED_KEY, {str(job_id): time.time() + delay})
    else:
        reason = "max_attempts" if is_retryable(error) else "non_retryable"
        await _bury(pool, redis, job_id, worker_id, attempts, str(error), reason)


async def handle_message(
    pool: asyncpg.Pool,
    redis: Redis,
    provider: MockLLM,
    worker_id: str,
    msg_id: str,
    fields: dict,
    policy: RetryPolicy | None = None,
) -> None:
    policy = policy or RetryPolicy()
    job_id = UUID(fields["job_id"])

    status = await pool.fetchval("SELECT status FROM jobs WHERE id = $1", job_id)
    if status is None or status in ("succeeded", "dead"):
        await redis.xack(STREAM, GROUP, msg_id)
        return

    claim = await _claim(pool, job_id, worker_id)
    if claim is None:
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
        # through, so a killed worker never reaches an XACK.
        await _handle_error(pool, redis, policy, job_id, worker_id, claim, e)
        # The outcome (retry scheduled in Redis, or dead + DLQ) is durable; now acknowledge.
        await redis.xack(STREAM, GROUP, msg_id)
        return

    await _complete(pool, job_id, worker_id, output)
    # Commit happened above; only now acknowledge. A crash right here means redelivery, which
    # the guarded UPDATE turns into a no-op.
    await redis.xack(STREAM, GROUP, msg_id)


async def move_due_jobs(
    pool: asyncpg.Pool, redis: Redis, now: float | None = None, batch: int = 100
) -> int:
    """Move retries whose time has come from the delayed set back onto the stream.

    XADD first, ZREM second: a crash in between re-delivers the job (a safe duplicate) instead of
    losing it. The job row is the source for the stream fields, so they cannot go stale.
    """
    now = time.time() if now is None else now
    due = await redis.zrangebyscore(DELAYED_KEY, "-inf", now, start=0, num=batch)
    moved = 0
    for job_id in due:
        row = await pool.fetchrow(
            "SELECT id, type, payload, status FROM jobs WHERE id = $1", UUID(job_id)
        )
        if row is not None and row["status"] == "retrying":
            await redis.xadd(
                STREAM, {"job_id": job_id, "type": row["type"], "payload": row["payload"]}
            )
            moved += 1
        await redis.zrem(DELAYED_KEY, job_id)
    return moved


async def run_delayed_mover(
    pool: asyncpg.Pool, redis: Redis, stop: asyncio.Event, interval: float = 0.5
) -> None:
    while not stop.is_set():
        try:
            await move_due_jobs(pool, redis)
        except Exception:
            log.exception("delayed mover cycle failed; retrying")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


async def worker_loop(
    worker_id: str,
    pool: asyncpg.Pool,
    redis: Redis,
    provider: MockLLM,
    stop: asyncio.Event,
    block_ms: int = 500,
    policy: RetryPolicy | None = None,
) -> None:
    while not stop.is_set():
        try:
            resp = await redis.xreadgroup(GROUP, worker_id, {STREAM: ">"}, count=1, block=block_ms)
            for _stream, entries in resp or []:
                for msg_id, fields in entries:
                    await handle_message(pool, redis, provider, worker_id, msg_id, fields, policy)
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
        policy: RetryPolicy | None = None,
        mover_interval: float = 0.5,
    ):
        self.pool, self.redis, self.provider = pool, redis, provider
        self.size, self.block_ms = size, block_ms
        self.policy = policy or RetryPolicy()
        self.mover_interval = mover_interval
        self.mover: asyncio.Task | None = None
        self.tasks: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await ensure_group(self.redis)
        for i in range(1, self.size + 1):
            wid = f"worker-{i}"
            self.tasks[wid] = asyncio.create_task(
                worker_loop(
                    wid,
                    self.pool,
                    self.redis,
                    self.provider,
                    self._stop,
                    self.block_ms,
                    self.policy,
                ),
                name=wid,
            )
        self.mover = asyncio.create_task(
            run_delayed_mover(self.pool, self.redis, self._stop, self.mover_interval),
            name="delayed-mover",
        )

    async def kill_worker(self, worker_id: str) -> None:
        """Simulate a crash: cancel the task mid-flight. Its message stays unacknowledged."""
        task = self.tasks.pop(worker_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def stop(self) -> None:
        """Graceful shutdown: workers finish their current message, then exit."""
        self._stop.set()
        pending = list(self.tasks.values()) + ([self.mover] if self.mover else [])
        await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()
        self.mover = None


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
