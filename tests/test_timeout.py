"""A hung provider call must end in a retryable timeout, not hold the job forever."""

import asyncio

from helpers import _acked, _events, _make_job, _wait_for

from app.dispatcher import publish_pending
from app.providers.mock_llm import MockLLM
from app.retry import RetryPolicy
from app.worker import WorkerPool

FAST = RetryPolicy(base=0.01, cap=0.05)


class HangsOnce(MockLLM):
    def __init__(self):
        super().__init__(min_latency=0, max_latency=0)
        self.calls = 0

    async def complete(self, prompt, **kwargs):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(30)  # far longer than the timeout below
        return {"text": "ok"}


def _workers(pool, redis, provider):
    return WorkerPool(
        pool, redis, provider, size=1, block_ms=50, policy=FAST, mover_interval=0.02,
        provider_timeout=0.1,
    )  # fmt: skip


async def test_hung_call_times_out_is_retried_and_succeeds(client, pool, redis):
    job_id = await _make_job(client, "k1")
    await publish_pending(pool, redis)
    provider = HangsOnce()

    workers = _workers(pool, redis, provider)
    await workers.start()
    try:
        await _wait_for(
            lambda: pool.fetchval(
                "SELECT status = 'succeeded' FROM jobs WHERE id = $1::uuid", job_id
            )
        )
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()

    assert provider.calls == 2
    assert await _events(pool, job_id) == [
        "job.started",
        "job.retry_scheduled",
        "job.started",
        "job.succeeded",
    ]
    error = await pool.fetchval(
        "SELECT payload->>'error' FROM events WHERE type = 'job.retry_scheduled'"
    )
    assert error == "provider call exceeded 0.1s"


async def test_mock_hang_mode_always_times_out_and_ends_in_the_dlq(client, pool, redis):
    job_id = await _make_job(client, "k1", {"prompt": "x", "mock_mode": "hang"}, max_attempts=2)
    await publish_pending(pool, redis)

    workers = _workers(pool, redis, MockLLM(min_latency=0, max_latency=0))
    await workers.start()
    try:
        await _wait_for(
            lambda: pool.fetchval("SELECT status = 'dead' FROM jobs WHERE id = $1::uuid", job_id)
        )
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()

    assert await _events(pool, job_id) == [
        "job.started",
        "job.retry_scheduled",
        "job.started",
        "job.dead",
    ]
