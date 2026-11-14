"""Chaos scenario: many jobs, a worker pool, and workers killed and replaced while jobs run.

Used by the end-to-end test (tests/test_chaos.py) and by scripts/demo_chaos.py, so the README
numbers come from the same code CI checks.

The scenario only measures. It does not decide pass or fail: the test asserts on ChaosResult.
"""

import asyncio
import contextlib
import json
import random
import time
from dataclasses import dataclass, field

import asyncpg
from redis.asyncio import Redis

from app.dispatcher import STREAM, run_dispatcher
from app.providers.mock_llm import MockLLM
from app.recovery import RecoveryConfig
from app.retry import RetryPolicy
from app.worker import GROUP, WorkerPool

# Short clocks so the run takes seconds. heartbeat_ttl < lease, as in production.
CHAOS_RECOVERY = RecoveryConfig(lease=0.6, interval=0.1, heartbeat_interval=0.05, heartbeat_ttl=0.4)


@dataclass
class ChaosResult:
    jobs: int
    succeeded: int = 0
    dead: int = 0
    outputs: int = 0  # rows in job_outputs for these jobs
    succeeded_events: dict[str, int] = field(default_factory=dict)  # job id -> job.succeeded count
    retries: int = 0  # job.retry_scheduled events (provider errors)
    recoveries: int = 0  # job.recovered events (a dead worker's job taken over)
    extra_attempts: int = 0  # attempts beyond the first, summed over jobs
    kills: int = 0
    pending: int = 0  # XPENDING after the run
    duration: float = 0.0

    @property
    def one_output_per_job(self) -> bool:
        return self.outputs == self.jobs

    @property
    def one_event_per_job(self) -> bool:
        return len(self.succeeded_events) == self.jobs and all(
            n == 1 for n in self.succeeded_events.values()
        )


async def submit_jobs(pool: asyncpg.Pool, count: int, run_id: str, max_attempts: int) -> list[str]:
    """Insert jobs and their outbox rows (what POST /jobs does), in ONE transaction."""
    async with pool.acquire() as conn, conn.transaction():
        ids = []
        for i in range(count):
            row = await conn.fetchrow(
                "INSERT INTO jobs (type, payload, idempotency_key, max_attempts) "
                "VALUES ('llm.mock', $1::jsonb, $2, $3) RETURNING id",
                json.dumps({"prompt": f"chaos {i}"}),
                f"{run_id}-{i}",
                max_attempts,
            )
            await conn.execute("INSERT INTO outbox (job_id) VALUES ($1)", row["id"])
            ids.append(row["id"])
        return ids


async def _kill_and_replace(
    workers: WorkerPool, pool: asyncpg.Pool, rng: random.Random, next_id: int
) -> None:
    """Kill a random worker (preferring one that is mid-job, so recovery really happens) and start
    a replacement under a new id, keeping the pool size constant."""
    busy = await pool.fetch("SELECT DISTINCT locked_by FROM jobs WHERE status = 'running'")
    candidates = [r["locked_by"] for r in busy if r["locked_by"] in workers.tasks]
    victim = rng.choice(candidates or list(workers.tasks))
    await workers.kill_worker(victim)
    await workers.start_worker(f"worker-{next_id}")


async def run_chaos(
    pool: asyncpg.Pool,
    redis: Redis,
    jobs: int = 200,
    workers: int = 4,
    failure_rate: float = 0.0,
    kill_every: tuple[float, float] = (0.15, 0.4),
    timeout: float = 120.0,
    seed: int | None = None,
    recovery: RecoveryConfig = CHAOS_RECOVERY,
) -> ChaosResult:
    """Submit `jobs` mock jobs, run `workers` workers, and kill/replace one every `kill_every`
    seconds (random in that range) until every job is finished or `timeout` passes."""
    rng = random.Random(seed)
    run_id = f"chaos-{time.time_ns()}"
    started = time.monotonic()

    # max_attempts=10 (the API maximum): a takeover counts as an attempt, and a few unlucky
    # kills of the same job must not push a healthy job into the DLQ.
    job_ids = await submit_jobs(pool, jobs, run_id, max_attempts=10)

    provider = MockLLM(failure_rate=failure_rate, min_latency=0.02, max_latency=0.08, rng=rng)
    pool_of_workers = WorkerPool(
        pool,
        redis,
        provider,
        size=workers,
        block_ms=50,
        policy=RetryPolicy(base=0.05, cap=0.5),
        mover_interval=0.05,
        recovery=recovery,
    )
    stop_dispatcher = asyncio.Event()
    dispatcher = asyncio.create_task(run_dispatcher(pool, redis, stop_dispatcher, interval=0.05))
    result = ChaosResult(jobs=jobs)

    await pool_of_workers.start()
    try:
        next_id = workers + 1
        deadline = started + timeout
        while time.monotonic() < deadline:
            unfinished = await pool.fetchval(
                "SELECT count(*) FROM jobs WHERE id = ANY($1) AND status NOT IN ('succeeded', 'dead')",
                job_ids,
            )
            if unfinished == 0:
                break
            await asyncio.sleep(rng.uniform(*kill_every))
            await _kill_and_replace(pool_of_workers, pool, rng, next_id)
            next_id += 1
            result.kills += 1

        # Survivors finish by acknowledging messages whose jobs are already done (e.g. a worker
        # killed between commit and XACK), so give XPENDING time to drain.
        drain_deadline = time.monotonic() + 15
        while time.monotonic() < drain_deadline:
            if (await redis.xpending(STREAM, GROUP))["pending"] == 0:
                break
            await asyncio.sleep(0.1)
    finally:
        await pool_of_workers.stop()
        stop_dispatcher.set()
        with contextlib.suppress(Exception):
            await dispatcher

    result.duration = time.monotonic() - started
    await _collect(pool, redis, job_ids, result)
    return result


async def _collect(pool: asyncpg.Pool, redis: Redis, job_ids: list, result: ChaosResult) -> None:
    rows = await pool.fetch("SELECT status, attempts FROM jobs WHERE id = ANY($1)", job_ids)
    result.succeeded = sum(r["status"] == "succeeded" for r in rows)
    result.dead = sum(r["status"] == "dead" for r in rows)
    result.extra_attempts = sum(max(r["attempts"] - 1, 0) for r in rows)
    result.outputs = await pool.fetchval(
        "SELECT count(*) FROM job_outputs WHERE job_id = ANY($1)", job_ids
    )
    events = await pool.fetch(
        "SELECT type, payload->>'job_id' AS job_id FROM events "
        "WHERE payload->>'job_id' = ANY($1::text[])",
        [str(j) for j in job_ids],
    )
    for e in events:
        if e["type"] == "job.succeeded":
            result.succeeded_events[e["job_id"]] = result.succeeded_events.get(e["job_id"], 0) + 1
        elif e["type"] == "job.retry_scheduled":
            result.retries += 1
        elif e["type"] == "job.recovered":
            result.recoveries += 1
    result.pending = (await redis.xpending(STREAM, GROUP))["pending"]
