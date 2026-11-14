"""Retry, backoff and DLQ behaviour against real Postgres and Redis."""

import time

from helpers import _acked, _events, _make_job, _pending, _wait_for

from app.dispatcher import STREAM, publish_pending
from app.dlq import replay_dlq
from app.providers.mock_llm import MockLLM, ProviderError
from app.retry import DELAYED_KEY, DLQ_STREAM, RetryPolicy
from app.worker import WorkerPool

FAST = RetryPolicy(base=0.01, cap=0.05)  # tiny delays so tests stay quick


class Scripted(MockLLM):
    """Fails the first `fail_times` calls with `status`, then succeeds. None = always fail."""

    def __init__(self, fail_times: int | None = None, status: int = 500):
        super().__init__(min_latency=0, max_latency=0)
        self.fail_times, self.status, self.calls = fail_times, status, 0

    async def complete(self, prompt, **kwargs):
        self.calls += 1
        if self.fail_times is None or self.calls <= self.fail_times:
            raise ProviderError(self.status, f"scripted {self.status}")
        return {"text": "ok"}


def _pool(pool, redis, provider, size=2, policy=FAST):
    return WorkerPool(pool, redis, provider, size=size, block_ms=100, policy=policy,
                      mover_interval=0.02)  # fmt: skip


async def _status(pool, job_id: str) -> str:
    return await pool.fetchval("SELECT status FROM jobs WHERE id = $1::uuid", job_id)


async def _job_status_is(pool, job_id: str, status: str) -> bool:
    return await _status(pool, job_id) == status


async def test_fails_twice_then_succeeds(client, pool, redis):
    job_id = await _make_job(client, "k1")
    await publish_pending(pool, redis)
    provider = Scripted(fail_times=2, status=429)

    workers = _pool(pool, redis, provider)
    await workers.start()
    try:
        await _wait_for(lambda: _job_status_is(pool, job_id, "succeeded"))
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()

    assert await pool.fetchval("SELECT attempts FROM jobs WHERE id = $1::uuid", job_id) == 3
    assert provider.calls == 3
    assert await _events(pool, job_id) == [
        "job.started",
        "job.retry_scheduled",
        "job.started",
        "job.retry_scheduled",
        "job.started",
        "job.succeeded",
    ]
    assert await pool.fetchval("SELECT count(*) FROM job_outputs") == 1
    assert await redis.zcard(DELAYED_KEY) == 0
    assert await redis.xlen(DLQ_STREAM) == 0


async def test_always_failing_job_ends_dead_in_dlq(client, pool, redis):
    job_id = await _make_job(client, "k1", max_attempts=3)
    await publish_pending(pool, redis)

    workers = _pool(pool, redis, Scripted(status=500))
    await workers.start()
    try:
        await _wait_for(lambda: _job_status_is(pool, job_id, "dead"))
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()

    job = await pool.fetchrow("SELECT attempts, max_attempts FROM jobs WHERE id = $1::uuid", job_id)
    assert (job["attempts"], job["max_attempts"]) == (3, 3)
    assert await _events(pool, job_id) == [
        "job.started",
        "job.retry_scheduled",
        "job.started",
        "job.retry_scheduled",
        "job.started",
        "job.dead",
    ]
    dlq = await redis.xrange(DLQ_STREAM)
    assert len(dlq) == 1
    assert dlq[0][1]["job_id"] == job_id
    assert dlq[0][1]["reason"] == "max_attempts"
    assert await _pending(redis) == 0
    assert await redis.zcard(DELAYED_KEY) == 0
    assert await pool.fetchval("SELECT count(*) FROM job_outputs") == 0


async def test_non_retryable_error_goes_straight_to_dlq(client, pool, redis):
    job_id = await _make_job(client, "k1", {"prompt": "x", "mock_mode": "400", "mock_latency": 0})
    await publish_pending(pool, redis)

    workers = _pool(pool, redis, MockLLM())
    await workers.start()
    try:
        await _wait_for(lambda: _job_status_is(pool, job_id, "dead"))
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()

    assert await pool.fetchval("SELECT attempts FROM jobs WHERE id = $1::uuid", job_id) == 1
    assert await _events(pool, job_id) == ["job.started", "job.dead"]
    assert (await redis.xrange(DLQ_STREAM))[0][1]["reason"] == "non_retryable"


async def test_replay_returns_dead_job_to_the_queue_and_it_can_succeed(client, pool, redis):
    job_id = await _make_job(client, "k1", max_attempts=2)
    await publish_pending(pool, redis)
    provider = Scripted(status=500)  # fails until we flip it below

    workers = _pool(pool, redis, provider)
    await workers.start()
    try:
        await _wait_for(lambda: _job_status_is(pool, job_id, "dead"))
        await _wait_for(lambda: _acked(redis))

        provider.fail_times = 0  # the "outage" is over
        assert await replay_dlq(pool, redis, job_id) is True
        assert await _status(pool, job_id) == "queued"
        assert await pool.fetchval("SELECT attempts FROM jobs WHERE id = $1::uuid", job_id) == 0
        assert await redis.xlen(DLQ_STREAM) == 0
        assert await publish_pending(pool, redis) == 1  # outbox -> stream

        await _wait_for(lambda: _job_status_is(pool, job_id, "succeeded"))
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()

    assert await pool.fetchval("SELECT attempts FROM jobs WHERE id = $1::uuid", job_id) == 1
    assert "job.replayed" in await _events(pool, job_id)
    assert await pool.fetchval("SELECT count(*) FROM job_outputs") == 1
    # Only a dead job can be replayed.
    assert await replay_dlq(pool, redis, job_id) is False


async def test_delayed_retry_does_not_block_other_jobs(client, pool, redis):
    class MaxRng:  # always the longest delay in the window
        def uniform(self, a, b):
            return b

    slow_retry = RetryPolicy(base=2.0, cap=2.0, rng=MaxRng())
    failing = await _make_job(client, "bad", {"prompt": "A", "mock_mode": "500", "mock_latency": 0})
    await publish_pending(pool, redis)

    # ONE worker: if the retry delay blocked it, the second job could not run.
    workers = _pool(pool, redis, MockLLM(min_latency=0, max_latency=0), size=1, policy=slow_retry)
    await workers.start()
    try:
        await _wait_for(lambda: _job_status_is(pool, failing, "retrying"))
        await _wait_for(lambda: redis.zcard(DELAYED_KEY))

        good = await _make_job(client, "good", {"prompt": "B"})
        await publish_pending(pool, redis)
        await _wait_for(lambda: _job_status_is(pool, good, "succeeded"), timeout=1.0)

        # The failing job is still waiting out its ~2s delay.
        assert await _status(pool, failing) == "retrying"
        score = (await redis.zrange(DELAYED_KEY, 0, -1, withscores=True))[0][1]
        assert score > time.time() + 1
        assert await redis.xlen(STREAM) == 2  # the retry has not been re-enqueued yet
    finally:
        await workers.stop()
