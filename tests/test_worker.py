"""Worker tests against real Postgres and Redis (no mocks of either)."""

import asyncio
import time

from app.dispatcher import STREAM, publish_pending
from app.providers.mock_llm import MockLLM
from app.worker import GROUP, WorkerPool


async def _make_job(client, key: str, payload: dict | None = None) -> str:
    r = await client.post(
        "/jobs",
        json={"type": "llm.mock", "payload": payload or {"prompt": "hi"}},
        headers={"Idempotency-Key": key},
    )
    return r.json()["id"]


async def _wait_for(check, timeout: float = 5.0):
    """Poll an async predicate until it returns something truthy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def _pending(redis) -> int:
    return (await redis.xpending(STREAM, GROUP))["pending"]


async def _acked(redis) -> bool:
    return await _pending(redis) == 0


async def _events(pool, job_id: str) -> list[str]:
    rows = await pool.fetch(
        "SELECT type FROM events WHERE payload->>'job_id' = $1 ORDER BY id", job_id
    )
    return [r["type"] for r in rows]


class CountingMock(MockLLM):
    def __init__(self):
        super().__init__(min_latency=0, max_latency=0)
        self.calls = 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        return await super().complete(*args, **kwargs)


async def test_happy_path(client, pool, redis):
    job_id = await _make_job(client, "k1")
    assert await publish_pending(pool, redis) == 1  # outbox -> dispatcher -> stream

    workers = WorkerPool(pool, redis, CountingMock(), size=2, block_ms=100)
    await workers.start()
    try:
        await _wait_for(
            lambda: pool.fetchval(
                "SELECT status = 'succeeded' FROM jobs WHERE id = $1::uuid", job_id
            )
        )
        # XACK happens just after the commit, so wait for it rather than assuming.
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()

    job = await pool.fetchrow("SELECT * FROM jobs WHERE id = $1::uuid", job_id)
    assert job["status"] == "succeeded"
    assert job["attempts"] == 1
    assert job["locked_by"] in ("worker-1", "worker-2")
    assert job["started_at"] is not None and job["completed_at"] is not None
    assert await _events(pool, job_id) == ["job.started", "job.succeeded"]
    assert (
        await pool.fetchval("SELECT count(*) FROM job_outputs WHERE job_id = $1::uuid", job_id) == 1
    )
    assert await _pending(redis) == 0


async def test_duplicate_delivery_runs_provider_once(client, pool, redis):
    job_id = await _make_job(client, "k1")
    assert await publish_pending(pool, redis) == 1
    # Deliver the same job a second time, as a dispatcher republish or redelivery would.
    first_fields = (await redis.xrange(STREAM))[0][1]
    await redis.xadd(STREAM, first_fields)
    assert await redis.xlen(STREAM) == 2

    provider = CountingMock()
    # One worker so the two deliveries are handled one after the other. (Two workers could run a
    # still-'running' duplicate at the same time; completion stays single, see DECISIONS.md #5.)
    workers = WorkerPool(pool, redis, provider, size=1, block_ms=100)
    await workers.start()
    try:

        async def both_delivered_and_acked() -> bool:
            last_id = (await redis.xrange(STREAM))[-1][0]
            group = (await redis.xinfo_groups(STREAM))[0]
            return group["last-delivered-id"] == last_id and group["pending"] == 0

        await _wait_for(both_delivered_and_acked)
    finally:
        await workers.stop()

    assert provider.calls == 1
    assert await _events(pool, job_id) == ["job.started", "job.succeeded"]
    assert await pool.fetchval("SELECT attempts FROM jobs WHERE id = $1::uuid", job_id) == 1
    assert await pool.fetchval("SELECT count(*) FROM job_outputs") == 1


async def test_killed_worker_leaves_job_running_and_message_pending(client, pool, redis):
    # The payload asks the mock to sleep 30s, so the worker is mid-call when we kill it.
    job_id = await _make_job(client, "k1", {"prompt": "slow", "mock_latency": 30})
    assert await publish_pending(pool, redis) == 1

    workers = WorkerPool(pool, redis, MockLLM(), size=2, block_ms=100)
    await workers.start()
    try:
        owner = await _wait_for(
            lambda: pool.fetchval(
                "SELECT locked_by FROM jobs WHERE id = $1::uuid AND status = 'running'", job_id
            )
        )
        await workers.kill_worker(owner)

        assert (
            await pool.fetchval("SELECT status FROM jobs WHERE id = $1::uuid", job_id) == "running"
        )
        assert await _pending(redis) == 1
        assert await _events(pool, job_id) == ["job.started"]
        assert await pool.fetchval("SELECT count(*) FROM job_outputs") == 0
    finally:
        await workers.stop()


async def test_provider_error_marks_job_failed_and_acks(client, pool, redis):
    job_id = await _make_job(client, "k1", {"prompt": "x", "mock_mode": "429"})
    assert await publish_pending(pool, redis) == 1

    workers = WorkerPool(pool, redis, MockLLM(min_latency=0, max_latency=0), size=1, block_ms=100)
    await workers.start()
    try:
        await _wait_for(lambda: _failed(pool, job_id))
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()

    assert await _events(pool, job_id) == ["job.started", "job.failed"]
    error = await pool.fetchval("SELECT payload->>'error' FROM events WHERE type = 'job.failed'")
    assert "429" in error
    assert await pool.fetchval("SELECT count(*) FROM job_outputs") == 0


async def _failed(pool, job_id: str) -> bool:
    return await pool.fetchval("SELECT status = 'failed' FROM jobs WHERE id = $1::uuid", job_id)


async def test_ensure_group_is_idempotent(redis):
    from app.worker import ensure_group

    await ensure_group(redis)
    await ensure_group(redis)  # BUSYGROUP is ignored
    assert [g["name"] for g in await redis.xinfo_groups(STREAM)] == [GROUP]
