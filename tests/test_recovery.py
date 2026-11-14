"""Crash recovery against real Postgres and Redis: heartbeats, leases, XAUTOCLAIM."""

import asyncio

from helpers import CountingMock, _acked, _events, _make_job, _wait_for

from app.dispatcher import STREAM, publish_pending
from app.providers.mock_llm import MockLLM
from app.recovery import RecoveryConfig
from app.worker import GROUP, WorkerPool

# Short clocks so tests are quick. heartbeat_ttl < lease, as in production.
FAST = RecoveryConfig(lease=0.3, interval=0.05, heartbeat_interval=0.05, heartbeat_ttl=0.2)


class SlowFirstCall(MockLLM):
    """The first call hangs (the worker will be killed during it); later calls return at once."""

    def __init__(self):
        super().__init__(min_latency=0, max_latency=0)
        self.calls = 0

    async def complete(self, prompt, **kwargs):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(30)
        return {"text": "ok"}


def _pool(pool, redis, provider, **kw):
    return WorkerPool(pool, redis, provider, size=2, block_ms=50, recovery=FAST, **kw)


async def _row(pool, job_id: str):
    return await pool.fetchrow("SELECT * FROM jobs WHERE id = $1::uuid", job_id)


async def _locked_by(pool, job_id: str):
    return await pool.fetchval(
        "SELECT locked_by FROM jobs WHERE id = $1::uuid AND status = 'running'", job_id
    )


async def _succeeded(pool, job_id: str) -> bool:
    return (await _row(pool, job_id))["status"] == "succeeded"


async def test_killed_worker_job_is_recovered_and_succeeds_once(client, pool, redis):
    job_id = await _make_job(client, "k1")
    await publish_pending(pool, redis)
    provider = SlowFirstCall()

    workers = _pool(pool, redis, provider)
    await workers.start()
    try:
        victim = await _wait_for(lambda: _locked_by(pool, job_id))
        await workers.kill_worker(victim)  # dies mid-provider-call; heartbeat stops too

        await _wait_for(lambda: _succeeded(pool, job_id))  # the survivor takes it over
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()

    job = await _row(pool, job_id)
    assert job["attempts"] == 2 and job["locked_by"] != victim
    assert await _events(pool, job_id) == [
        "job.started",
        "job.recovered",
        "job.started",
        "job.succeeded",
    ]
    recovered = await pool.fetchval("SELECT payload FROM events WHERE type = 'job.recovered'")
    assert f'"previous_worker": "{victim}"' in recovered
    assert f'"new_worker": "{job["locked_by"]}"' in recovered
    assert await pool.fetchval("SELECT count(*) FROM job_outputs") == 1
    assert provider.calls == 2


async def test_crash_between_commit_and_xack_leaves_one_output_and_one_event(client, pool, redis):
    job_id = await _make_job(client, "k1")
    await publish_pending(pool, redis)
    provider = CountingMock()
    committed = asyncio.Event()

    async def crash_point(_job_id):
        # Runs after the completion COMMIT and before the XACK. Park here until we kill it.
        committed.set()
        await asyncio.sleep(3600)

    workers = _pool(pool, redis, provider, after_complete=crash_point)
    await workers.start()
    try:
        await asyncio.wait_for(committed.wait(), timeout=5)
        victim = (await _row(pool, job_id))["locked_by"]
        assert await _succeeded(pool, job_id)
        assert (await redis.xpending(STREAM, GROUP))["pending"] == 1  # committed, not acked
        await workers.kill_worker(victim)

        await _wait_for(lambda: _acked(redis))  # the survivor reclaims it and just acks
    finally:
        await workers.stop()

    assert await pool.fetchval("SELECT count(*) FROM job_outputs") == 1
    events = await _events(pool, job_id)
    assert events.count("job.succeeded") == 1
    assert events == ["job.started", "job.succeeded"]  # no second run, no recovery event
    assert provider.calls == 1


async def test_same_job_delivered_to_two_live_workers_runs_once(client, pool, redis):
    job_id = await _make_job(client, "k1", {"prompt": "dup", "mock_latency": 0.4})
    await publish_pending(pool, redis)
    await redis.xadd(STREAM, (await redis.xrange(STREAM))[0][1])  # same job, delivered twice
    provider = CountingMock()

    workers = _pool(pool, redis, provider)
    await workers.start()
    try:

        async def both_delivered() -> bool:
            last_id = (await redis.xrange(STREAM))[-1][0]
            return (await redis.xinfo_groups(STREAM))[0]["last-delivered-id"] == last_id

        await _wait_for(both_delivered)
        await _wait_for(lambda: _succeeded(pool, job_id))
        await _wait_for(lambda: _acked(redis))  # the skipped duplicate is acked once job is done
    finally:
        await workers.stop()

    assert provider.calls == 1
    assert await _events(pool, job_id) == ["job.started", "job.succeeded"]
    assert (await _row(pool, job_id))["attempts"] == 1


async def test_job_with_heartbeating_worker_is_not_stolen_after_lease(client, pool, redis):
    # The job runs 1.0s, more than three leases (0.3s). The owner heartbeats the whole time.
    job_id = await _make_job(client, "k1", {"prompt": "long", "mock_latency": 1.0})
    await publish_pending(pool, redis)
    provider = CountingMock()

    workers = _pool(pool, redis, provider)
    await workers.start()
    try:
        owner = await _wait_for(lambda: _locked_by(pool, job_id))

        async def recovery_tried_to_steal() -> bool:
            pending = await redis.xpending_range(STREAM, GROUP, min="-", max="+", count=10)
            return bool(pending) and pending[0]["times_delivered"] >= 2

        await _wait_for(recovery_tried_to_steal)  # XAUTOCLAIM did hand it to the other worker...
        row = await _row(pool, job_id)
        assert row["status"] == "running" and row["locked_by"] == owner  # ...which left it alone
        assert "job.recovered" not in await _events(pool, job_id)

        await _wait_for(lambda: _succeeded(pool, job_id), timeout=5)
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()

    assert provider.calls == 1
    assert await _events(pool, job_id) == ["job.started", "job.succeeded"]
    assert (await _row(pool, job_id))["attempts"] == 1
