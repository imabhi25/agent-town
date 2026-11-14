"""Worker pool: N asyncio tasks reading the jobs stream through one Redis consumer group.

Delivery is at-least-once. A job can arrive twice (dispatcher republish, redelivery after a
crash), so every step is guarded in Postgres and the message is XACKed only AFTER the database
outcome is committed. See DECISIONS.md #1 and #5.

Cancellation rule: nothing here catches CancelledError and no XACK sits in a `finally`. A killed
worker therefore leaves its message unacknowledged (pending); another worker reclaims it with
XAUTOCLAIM once the lease has expired and the dead worker's heartbeat is gone. See DECISIONS.md #7.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from uuid import UUID

import asyncpg
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.config import settings
from app.dispatcher import STREAM
from app.events import add_event
from app.providers.mock_llm import MockLLM, ProviderError
from app.recovery import RecoveryConfig, beat, claim_stale, heartbeat_key, heartbeat_loop, is_alive
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


async def _claim(
    pool: asyncpg.Pool, redis: Redis, job_id: UUID, worker_id: str
) -> tuple[str, asyncpg.Record | None]:
    """Decide what to do with a delivered job, in ONE transaction holding the job's row lock.

    Returns (outcome, row):
    - "finished": job is gone, succeeded or dead. Caller XACKs.
    - "busy": job is running under ANOTHER worker that is still heartbeating. Caller leaves the
      message pending (no XACK) and does not run the provider.
    - "claimed": this worker now owns the job; row has (attempts, max_attempts).

    FOR UPDATE makes two workers claiming the same job take turns: the second sees the first's
    lock and finds its owner alive. If the previous owner is dead (no heartbeat), or the lock is
    this worker's own (a worker handles one message at a time, so it cannot still be running it:
    a restarted worker reusing its id), we take over and record job.recovered.
    """
    async with pool.acquire() as conn, conn.transaction():
        job = await conn.fetchrow(
            "SELECT status, locked_by FROM jobs WHERE id = $1 FOR UPDATE", job_id
        )
        if job is None or job["status"] in ("succeeded", "dead"):
            return "finished", None

        previous = job["locked_by"]
        took_over = job["status"] == "running" and previous is not None
        if took_over and previous != worker_id and await is_alive(redis, previous):
            return "busy", None

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
        if took_over:
            await add_event(
                conn,
                "job.recovered",
                {"job_id": str(job_id), "previous_worker": previous, "new_worker": worker_id},
            )
        await add_event(conn, "job.started", {"job_id": str(job_id), "worker": worker_id})
        return "claimed", row


async def _complete(pool: asyncpg.Pool, job_id: UUID, worker_id: str, output: dict) -> None:
    """Record success in ONE transaction. If another delivery already won, write nothing."""
    async with pool.acquire() as conn, conn.transaction():
        updated = await conn.fetchval(
            """
            UPDATE jobs SET status = 'succeeded', completed_at = now(), updated_at = now()
            WHERE id = $1 AND status NOT IN ('succeeded', 'dead')
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
    after_complete: Callable[[UUID], Awaitable[None]] | None = None,
    provider_timeout: float | None = None,
) -> None:
    """`after_complete` is a test hook awaited between the completion commit and the XACK."""
    policy = policy or RetryPolicy()
    provider_timeout = settings.provider_timeout if provider_timeout is None else provider_timeout
    job_id = UUID(fields["job_id"])

    outcome, claim = await _claim(pool, redis, job_id, worker_id)
    if outcome == "finished":
        await redis.xack(STREAM, GROUP, msg_id)
        return
    if outcome == "busy":
        # Another live worker is running this job. Do NOT ack: if that worker dies, the message
        # is still pending and gets reclaimed. If it finishes, the next claim sees 'succeeded'.
        return

    payload = json.loads(fields.get("payload") or "{}")
    try:
        # wait_for cancels a hung call and raises TimeoutError, which is retryable (retry.py).
        # Without it a hung call would hold the job locked forever (heartbeats only prove the
        # process is alive).
        output = await asyncio.wait_for(
            provider.complete(
                payload.get("prompt", ""),
                mode=payload.get("mock_mode"),
                latency=payload.get("mock_latency"),
            ),
            timeout=provider_timeout,
        )
    except (TimeoutError, ProviderError) as e:
        if isinstance(e, TimeoutError) and not str(e):
            e = TimeoutError(f"provider call exceeded {provider_timeout}s")  # wait_for's is blank
        # Only these two are caught. CancelledError is a BaseException and passes straight
        # through, so a killed worker never reaches an XACK.
        await _handle_error(pool, redis, policy, job_id, worker_id, claim, e)
        # The outcome (retry scheduled in Redis, or dead + DLQ) is durable; now acknowledge.
        await redis.xack(STREAM, GROUP, msg_id)
        return

    await _complete(pool, job_id, worker_id, output)
    if after_complete is not None:
        await after_complete(job_id)
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
    recovery: RecoveryConfig | None = None,
    after_complete: Callable[[UUID], Awaitable[None]] | None = None,
    provider_timeout: float | None = None,
) -> None:
    recovery = recovery or RecoveryConfig()
    last_recovery = 0.0
    while not stop.is_set():
        try:
            # Recovery step: every `interval`, take over messages that have been pending longer
            # than the lease (their worker died, or is still busy: _claim tells them apart).
            # The claimed messages go to THIS worker, which is alive by definition.
            if time.monotonic() - last_recovery >= recovery.interval:
                last_recovery = time.monotonic()
                for msg_id, fields in await claim_stale(
                    redis, STREAM, GROUP, worker_id, recovery.lease
                ):
                    await handle_message(
                        pool,
                        redis,
                        provider,
                        worker_id,
                        msg_id,
                        fields,
                        policy,
                        after_complete,
                        provider_timeout,
                    )

            resp = await redis.xreadgroup(GROUP, worker_id, {STREAM: ">"}, count=1, block=block_ms)
            for _stream, entries in resp or []:
                for msg_id, fields in entries:
                    await handle_message(
                        pool,
                        redis,
                        provider,
                        worker_id,
                        msg_id,
                        fields,
                        policy,
                        after_complete,
                        provider_timeout,
                    )
        except Exception:
            # Unexpected error: the message stays pending (no XACK) and is reclaimed after the lease.
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
        recovery: RecoveryConfig | None = None,
        after_complete: Callable[[UUID], Awaitable[None]] | None = None,
        provider_timeout: float | None = None,
    ):
        self.provider_timeout = provider_timeout
        self.pool, self.redis, self.provider = pool, redis, provider
        self.size, self.block_ms = size, block_ms
        self.policy = policy or RetryPolicy()
        self.mover_interval = mover_interval
        self.recovery = recovery or RecoveryConfig()
        self.after_complete = after_complete
        self.heartbeats: dict[str, asyncio.Task] = {}
        self.mover: asyncio.Task | None = None
        self.tasks: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await ensure_group(self.redis)
        for i in range(1, self.size + 1):
            await self.start_worker(f"worker-{i}")
        self.mover = asyncio.create_task(
            run_delayed_mover(self.pool, self.redis, self._stop, self.mover_interval),
            name="delayed-mover",
        )

    async def start_worker(self, wid: str) -> None:
        """Start one worker with its heartbeat. Also used to replace a killed worker."""
        # Beat once BEFORE the worker can read a message, so it never looks dead at start.
        await beat(self.redis, wid, self.recovery.heartbeat_ttl)
        self.heartbeats[wid] = asyncio.create_task(
            heartbeat_loop(
                self.redis,
                wid,
                self._stop,
                self.recovery.heartbeat_interval,
                self.recovery.heartbeat_ttl,
            ),
            name=f"{wid}-heartbeat",
        )
        self.tasks[wid] = asyncio.create_task(
            worker_loop(
                wid,
                self.pool,
                self.redis,
                self.provider,
                self._stop,
                self.block_ms,
                self.policy,
                self.recovery,
                self.after_complete,
                self.provider_timeout,
            ),
            name=wid,
        )

    async def kill_worker(self, worker_id: str) -> None:
        """Simulate a crash: cancel the worker AND its heartbeat. The message stays unacknowledged
        and the heartbeat key is left to expire on its own, like a process that died."""
        tasks = [self.tasks.pop(worker_id), self.heartbeats.pop(worker_id)]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        """Graceful shutdown: workers finish their current message, then exit."""
        self._stop.set()
        pending = (
            list(self.tasks.values())
            + list(self.heartbeats.values())
            + ([self.mover] if self.mover else [])
        )
        await asyncio.gather(*pending, return_exceptions=True)
        # A graceful shutdown deregisters; a crash is left to expire.
        await self.redis.delete(*(heartbeat_key(w) for w in self.tasks))
        self.tasks.clear()
        self.heartbeats.clear()
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
