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
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.config import settings
from app.dispatcher import STREAM
from app.events import add_event
from app.goals import complete_task, job_died, store_plan
from app.jobs import clamp_latency
from app.planner import Planner, build_gemini_planner, make_plan
from app.protection import Protection, deferral_delay
from app.providers.mock_llm import MockLLM, ProviderError
from app.recovery import (
    RecoveryConfig,
    beat,
    claim_stale,
    heartbeat_key,
    heartbeat_loop,
    heartbeat_token,
    is_alive,
)
from app.retry import DELAYED_KEY, DLQ_STREAM, RetryPolicy, is_retryable
from app.sleep import SleepGate

GROUP = "workers"


@dataclass
class WorkerExtras:
    """Optional collaborators of a worker; all None = the plain behaviour."""

    planner: Planner | None = None  # real (Gemini) planner; None = mock planner only
    protection: Protection | None = None  # circuit breaker + provider token bucket
    gate: SleepGate | None = None  # free-tier live demo: sleep when idle
    idle_backoff_max: float = 0.0  # seconds; 0 = no extra back-off when the queue is empty
    # DELIBERATELY BROKEN, for the chaos campaign only (scripts/chaos_campaign.py --mode unsafe):
    # XACK as soon as the job is claimed, BEFORE the provider call and the completion commit.
    # A crash in between then loses the job. It exists so the checker can be shown to catch the
    # bug the real ordering prevents. Nothing in the app sets it.
    unsafe_ack_before_commit: bool = False


log = logging.getLogger("worker")


async def ensure_group(redis: Redis) -> None:
    """Create the consumer group (and the stream if missing). Safe to call repeatedly."""
    try:
        # id="0": a group created after jobs were published still receives them.
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def _held_by_other(
    redis: Redis,
    previous: str,
    previous_token: str | None,
    worker_id: str,
    lock_token: str | None,
) -> bool:
    """Is the job's lock held by a worker that is alive AND is the incarnation that took it?

    A heartbeat proves a process with that NAME is running, not that it still runs this job: a
    worker that crashed and came back under the same name beats again but has forgotten the job.
    Comparing the heartbeat's token with the token stored on the lock tells the two apart. Without
    it, a job locked by a restarted worker could be passed between live workers that each see it
    as "busy" and leave it alone, forever (found by the TLA+ model, spec/README.md).
    """
    if previous == worker_id and previous_token == lock_token:
        return False  # our own lock; we handle one message at a time, so it is stale
    if previous_token is None:  # locked by a worker without a token: name-only check
        return previous != worker_id and await is_alive(redis, previous)
    return await heartbeat_token(redis, previous) == previous_token


async def _claim(
    pool: asyncpg.Pool,
    redis: Redis,
    job_id: UUID,
    worker_id: str,
    lock_token: str | None = None,
) -> tuple[str, asyncpg.Record | None]:
    """Decide what to do with a delivered job, in ONE transaction holding the job's row lock.

    Returns (outcome, row):
    - "finished": job is gone, succeeded or dead. Caller XACKs.
    - "busy": job is running under ANOTHER live worker incarnation. Caller leaves the message
      pending (no XACK) and does not run the provider.
    - "claimed": this worker now owns the job; row has (attempts, max_attempts).

    FOR UPDATE makes two workers claiming the same job take turns: the second sees the first's
    lock and finds its owner alive. If the previous owner is dead (no heartbeat), restarted (its
    heartbeat carries a different token), or is this very worker, we take over and record
    job.recovered.
    """
    async with pool.acquire() as conn, conn.transaction():
        job = await conn.fetchrow(
            "SELECT status, locked_by, lock_token FROM jobs WHERE id = $1 FOR UPDATE", job_id
        )
        if job is None or job["status"] in ("succeeded", "dead"):
            return "finished", None

        previous = job["locked_by"]
        took_over = job["status"] == "running" and previous is not None
        if took_over and await _held_by_other(
            redis, previous, job["lock_token"], worker_id, lock_token
        ):
            return "busy", None

        row = await conn.fetchrow(
            """
            UPDATE jobs
            SET status = 'running', locked_by = $2, lock_token = $3, started_at = now(),
                updated_at = now(), attempts = attempts + 1
            WHERE id = $1 AND status NOT IN ('succeeded', 'dead')
            RETURNING attempts, max_attempts
            """,
            job_id,
            worker_id,
            lock_token,
        )
        if took_over:
            await add_event(
                conn,
                "job.recovered",
                {"job_id": str(job_id), "previous_worker": previous, "new_worker": worker_id},
            )
        await add_event(conn, "job.started", {"job_id": str(job_id), "worker": worker_id})
        return "claimed", row


async def _complete(
    pool: asyncpg.Pool,
    job_id: UUID,
    worker_id: str,
    output: dict,
    on_success: Callable[[asyncpg.Connection], Awaitable[None]] | None = None,
) -> None:
    """Record success in ONE transaction. If another delivery already won, write nothing.

    `on_success` runs inside the same transaction, and only for the delivery that wins the guarded
    UPDATE, so side effects of completion (such as storing a plan) happen exactly once in effect.
    """
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
        if on_success is not None:
            await on_success(conn)


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
            RETURNING type, payload, task_id
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
        # A dead task or planner job fails its goal, in the same transaction.
        await job_died(conn, row["type"], json.loads(row["payload"]), row["task_id"], error)
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
    gate: SleepGate | None = None,
) -> None:
    attempts = claim["attempts"]
    if is_retryable(error) and attempts < claim["max_attempts"]:
        delay = policy.delay(attempts)
        if await _schedule_retry(pool, job_id, worker_id, attempts, delay, str(error)):
            # The delay lives in Redis, not in this worker: the worker is free right away.
            await redis.zadd(DELAYED_KEY, {str(job_id): time.time() + delay})
            if gate is not None:
                gate.delayed.set()  # wake the retry mover if it is idling
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
    extras: WorkerExtras | None = None,
    lock_token: str | None = None,
) -> None:
    """`extras.planner` is the real (Gemini) planner or None for mock-only (type 'planner' jobs).
    `after_complete` is a test hook awaited between the completion commit and the XACK."""
    policy = policy or RetryPolicy()
    extras = extras or WorkerExtras()
    planner, protection, gate = extras.planner, extras.protection, extras.gate
    provider_timeout = settings.provider_timeout if provider_timeout is None else provider_timeout
    job_id = UUID(fields["job_id"])

    # Protect the provider BEFORE claiming: a refused job is parked, not failed, and does not
    # burn an attempt (claiming increments `attempts`). If the circuit is open or we are calling
    # too fast, the job returns via the delayed set once the wait has passed.
    provider_name = "planner" if fields.get("type") == "planner" else "llm"
    if protection is not None:
        admission = await protection.admit(provider_name)
        if not admission.ok:
            await redis.zadd(
                DELAYED_KEY, {str(job_id): time.time() + deferral_delay(admission.wait)}
            )
            if gate is not None:
                gate.delayed.set()
            await redis.xack(STREAM, GROUP, msg_id)
            return

    outcome, claim = await _claim(pool, redis, job_id, worker_id, lock_token)
    if outcome == "finished":
        await redis.xack(STREAM, GROUP, msg_id)
        return
    if outcome == "busy":
        # Another live worker is running this job. Do NOT ack: if that worker dies, the message
        # is still pending and gets reclaimed. If it finishes, the next claim sees 'succeeded'.
        return

    if extras.unsafe_ack_before_commit:
        await redis.xack(STREAM, GROUP, msg_id)  # the bug under test; see WorkerExtras

    payload = json.loads(fields.get("payload") or "{}")
    on_success = None
    try:
        # wait_for cancels a hung call and raises TimeoutError, which is retryable (retry.py).
        # Without it a hung call would hold the job locked forever (heartbeats only prove the
        # process is alive).
        if fields.get("type") == "planner":
            result = await asyncio.wait_for(
                make_plan(
                    payload.get("prompt", ""),
                    planner,
                    settings.planner,
                    payload.get("preset"),
                ),
                timeout=provider_timeout,
            )
            output = {"source": result.source, "tasks": [t.model_dump() for t in result.plan.tasks]}
            goal_id = UUID(payload["goal_id"])

            async def on_success(conn: asyncpg.Connection) -> None:
                await store_plan(conn, goal_id, result.plan, result.source)

        else:
            latency = payload.get("mock_latency")
            extra = (
                {"real": True}
                if fields.get("type") == "task" and getattr(provider, "routes_real", False)
                else {}
            )
            output = await asyncio.wait_for(
                provider.complete(
                    payload.get("prompt", ""),
                    mode=payload.get("mock_mode"),
                    # Defence in depth: whatever got into the payload, never sleep past 10s.
                    latency=None if latency is None else clamp_latency(latency),
                    **extra,
                ),
                timeout=provider_timeout,
            )
            if fields.get("type") == "task":
                task_id = UUID(payload["task_id"])

                async def on_success(conn: asyncpg.Connection) -> None:
                    await complete_task(conn, task_id, output)
    except (TimeoutError, ProviderError) as e:
        if isinstance(e, TimeoutError) and not str(e):
            e = TimeoutError(f"provider call exceeded {provider_timeout}s")  # wait_for's is blank
        # Only these two are caught. CancelledError is a BaseException and passes straight
        # through, so a killed worker never reaches an XACK.
        if protection is not None:
            # A retryable error means the provider is unhealthy; a non-retryable one (400) means
            # it answered fine and the request was bad, which counts as healthy.
            await protection.record(provider_name, success=not is_retryable(e))
        await _handle_error(pool, redis, policy, job_id, worker_id, claim, e, gate)
        # The outcome (retry scheduled in Redis, or dead + DLQ) is durable; now acknowledge.
        await redis.xack(STREAM, GROUP, msg_id)
        return

    if protection is not None:
        await protection.record(provider_name, success=True)
    await _complete(pool, job_id, worker_id, output, on_success)
    if after_complete is not None:
        await after_complete(job_id)
    # Commit happened above; only now acknowledge. A crash right here means redelivery, which
    # the guarded UPDATE turns into a no-op.
    await redis.xack(STREAM, GROUP, msg_id)


# ZREM only if the entry still has the score we read. A worker may have rescheduled the job
# (failed again, ZADD with a later score) between our XADD and our ZREM; a plain ZREM would then
# delete the NEW entry, and the job would sit in 'retrying' with nothing left to run it (found by
# the TLA+ model, spec/README.md). Scores are compared as numbers; repr() of a float round-trips.
_ZREM_IF_SCORE = """
local s = redis.call('ZSCORE', KEYS[1], ARGV[1])
if s and tonumber(s) == tonumber(ARGV[2]) then
  return redis.call('ZREM', KEYS[1], ARGV[1])
end
return 0
"""


async def move_due_jobs(
    pool: asyncpg.Pool, redis: Redis, now: float | None = None, batch: int = 100
) -> int:
    """Move delayed jobs (retries, and jobs parked by the circuit breaker or rate limit) whose
    time has come from the delayed set back onto the stream.

    XADD first, ZREM second: a crash in between re-delivers the job (a safe duplicate) instead of
    losing it. The ZREM is conditional on the score (see _ZREM_IF_SCORE). The job row is the
    source for the stream fields, so they cannot go stale.
    """
    now = time.time() if now is None else now
    due = await redis.zrangebyscore(DELAYED_KEY, "-inf", now, start=0, num=batch, withscores=True)
    moved = 0
    for job_id, score in due:
        row = await pool.fetchrow(
            "SELECT id, type, payload, status FROM jobs WHERE id = $1", UUID(job_id)
        )
        if row is not None and row["status"] not in ("succeeded", "dead"):
            await redis.xadd(
                STREAM, {"job_id": job_id, "type": row["type"], "payload": row["payload"]}
            )
            moved += 1
        await redis.eval(_ZREM_IF_SCORE, 1, DELAYED_KEY, job_id, repr(score))
    return moved


async def run_delayed_mover(
    pool: asyncpg.Pool,
    redis: Redis,
    stop: asyncio.Event,
    interval: float = 0.5,
    gate: SleepGate | None = None,
) -> None:
    """Poll the delayed set. With a live-demo gate the poll stops while nothing is delayed: the
    loop waits for `gate.delayed` (set whenever a retry or park is scheduled) or 60s, whichever
    comes first, so an idle system makes no Redis commands here."""
    while not stop.is_set():
        if gate is not None:
            await gate.wait_awake(stop)
        try:
            await move_due_jobs(pool, redis)
            if gate is not None and gate.enabled:
                gate.delayed.clear()  # clear BEFORE looking, so a concurrent zadd is not missed
                if await redis.zcard(DELAYED_KEY) == 0:
                    waiters = [
                        asyncio.create_task(gate.delayed.wait()),
                        asyncio.create_task(stop.wait()),
                    ]
                    await asyncio.wait(waiters, timeout=60, return_when=asyncio.FIRST_COMPLETED)
                    for w in waiters:
                        w.cancel()
                    await asyncio.gather(*waiters, return_exceptions=True)
                    continue
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
    extras: WorkerExtras | None = None,
    lock_token: str | None = None,
) -> None:
    recovery = recovery or RecoveryConfig()
    extras = extras or WorkerExtras()
    gate = extras.gate
    last_recovery = 0.0
    empty_reads = 0
    while not stop.is_set():
        try:
            if gate is not None:
                await gate.wait_awake(stop)  # asleep: no polling at all
                if stop.is_set():
                    break
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
                        extras,
                        lock_token,
                    )

            resp = await redis.xreadgroup(GROUP, worker_id, {STREAM: ">"}, count=1, block=block_ms)
            if resp:
                empty_reads = 0
            elif extras.idle_backoff_max > 0:
                # Idle back-off: the emptier the queue has been, the longer between reads (up to
                # the max). A new goal cuts the sleep short through the gate.
                empty_reads += 1
                pause = min(extras.idle_backoff_max, 0.5 * 2**empty_reads)
                if gate is not None:
                    await gate.idle_sleep(pause)
                else:
                    await asyncio.sleep(pause)
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
                        extras,
                        lock_token,
                    )
        except ResponseError as e:
            if "NOGROUP" in str(e):
                # Redis lost the stream or group (flush, eviction): recreate it and carry on.
                log.warning("%s: consumer group missing; recreating", worker_id)
                await ensure_group(redis)
            else:
                log.exception("%s: redis error; continuing", worker_id)
                await asyncio.sleep(0.5)
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
        planner: Planner | None = None,
        protection: Protection | None = None,
        gate: SleepGate | None = None,
        idle_backoff_max: float = 0.0,
    ):
        self.provider_timeout = provider_timeout
        self.gate = gate
        self.extras = WorkerExtras(planner, protection, gate, idle_backoff_max)
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
        self._next_number = size + 1

    def new_worker_id(self) -> str:
        """A fresh id for a replacement worker: ids are never reused within one pool."""
        while f"worker-{self._next_number}" in self.tasks:
            self._next_number += 1
        wid = f"worker-{self._next_number}"
        self._next_number += 1
        return wid

    async def start(self) -> None:
        await ensure_group(self.redis)
        for i in range(1, self.size + 1):
            await self.start_worker(f"worker-{i}")
        self.mover = asyncio.create_task(
            run_delayed_mover(self.pool, self.redis, self._stop, self.mover_interval, self.gate),
            name="delayed-mover",
        )

    async def start_worker(self, wid: str) -> None:
        """Start one worker with its heartbeat. Also used to replace a killed worker.

        Every start gets a fresh lock token, also when the name is reused (a process restart
        starts again at worker-1): see _held_by_other."""
        token = uuid.uuid4().hex
        # Beat once BEFORE the worker can read a message, so it never looks dead at start.
        await beat(self.redis, wid, self.recovery.heartbeat_ttl, token)
        self.heartbeats[wid] = asyncio.create_task(
            heartbeat_loop(
                self.redis,
                wid,
                self._stop,
                self.recovery.heartbeat_interval,
                self.recovery.heartbeat_ttl,
                self.gate,
                token,
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
                self.extras,
                token,
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
        if self.tasks:  # DEL with no keys is an error (every worker may have been killed)
            await self.redis.delete(*(heartbeat_key(w) for w in self.tasks))
        self.tasks.clear()
        self.heartbeats.clear()
        self.mover = None


def build_worker_pool(
    pool: asyncpg.Pool, redis: Redis, gate: SleepGate | None = None
) -> "WorkerPool":
    """The production worker pool: real Gemini where configured (capped), circuit breaker and
    provider rate limit on, and the live-demo profile's back-off when LIVE_DEMO is set."""
    from app.providers.gemini_llm import build_provider

    # PLANNER=fixed means no Gemini anywhere: no client is built, so no key is needed or used.
    api_key = settings.gemini_api_key if settings.planner == "gemini" else ""
    planner = build_gemini_planner(
        redis, api_key, settings.gemini_model, settings.gemini_daily_cap
    )  # None without a key: the mock planner is used
    provider = build_provider(redis, api_key, settings.gemini_model, settings.gemini_daily_cap)
    return WorkerPool(
        pool,
        redis,
        provider,
        size=settings.worker_count,
        block_ms=settings.worker_block_ms,
        planner=planner,
        protection=Protection(pool, redis),
        gate=gate,
        idle_backoff_max=5.0 if settings.live_demo else 0.0,
    )


async def main() -> None:
    from app.db import create_pool
    from app.reconcile import reconcile
    from app.redis_budget import CountingRedis

    pool = await create_pool(min_size=1, max_size=settings.worker_count + 2)
    redis = CountingRedis.from_url(settings.redis_url, decode_responses=True)
    await reconcile(pool, redis)  # Redis may have lost jobs that Postgres still knows about
    workers = build_worker_pool(pool, redis)
    await workers.start()
    try:
        await asyncio.Event().wait()
    finally:
        await workers.stop()
        await redis.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
