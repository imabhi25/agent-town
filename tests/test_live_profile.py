"""Free-tier live profile (sleep gate, idle back-off, quiet mover/hub) and startup reconcile."""

import asyncio
import json
import time

from helpers import CountingMock, _make_job, _wait_for

from app.dispatcher import STREAM, publish_pending
from app.event_publisher import EVENTS_STREAM, publish_events
from app.goals import store_plan
from app.hub import EventHub
from app.planner import MockPlanner
from app.reconcile import reconcile
from app.sleep import SleepGate
from app.worker import GROUP, WorkerPool, run_delayed_mover

# --- sleep gate ----------------------------------------------------------------------------------


async def test_gate_sleeps_when_idle_and_wakes_on_touch():
    gate = SleepGate(enabled=True, idle_seconds=0.1)
    assert not gate.maybe_sleep()
    await asyncio.sleep(0.15)
    assert gate.maybe_sleep() and gate.sleeping
    stop = asyncio.Event()
    waiter = asyncio.create_task(gate.wait_awake(stop))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    gate.touch()
    await asyncio.wait_for(waiter, 1)
    assert not gate.sleeping


async def test_disabled_gate_never_sleeps_and_wait_awake_returns_on_stop():
    gate = SleepGate(enabled=False, idle_seconds=0)
    assert not gate.maybe_sleep()
    sleeping = SleepGate(enabled=True, idle_seconds=0)
    sleeping.maybe_sleep()
    stop = asyncio.Event()
    waiter = asyncio.create_task(sleeping.wait_awake(stop))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(waiter, 1)  # shutdown is not blocked by sleep


async def test_sleeping_workers_do_not_process_until_a_goal_wakes_them(client, pool, redis):
    gate = SleepGate(enabled=True, idle_seconds=0)
    job_id = await _make_job(client, "k")
    await publish_pending(pool, redis)
    gate.maybe_sleep()
    assert gate.sleeping
    workers = WorkerPool(pool, redis, CountingMock(), size=1, block_ms=50, gate=gate)
    await workers.start()
    try:
        await asyncio.sleep(0.6)
        assert (
            await pool.fetchval("SELECT status FROM jobs WHERE id = $1::uuid", job_id) == "queued"
        )
        gate.touch()  # a new goal arrives
        await _wait_for(
            lambda: pool.fetchval(
                "SELECT status = 'succeeded' FROM jobs WHERE id = $1::uuid", job_id
            )
        )
    finally:
        await workers.stop()


async def test_idle_back_off_is_cut_short_by_new_work(client, pool, redis):
    gate = SleepGate(enabled=False)
    workers = WorkerPool(
        pool, redis, CountingMock(), size=1, block_ms=50, gate=gate, idle_backoff_max=10
    )
    await workers.start()
    try:
        await asyncio.sleep(1.0)  # empty reads: the worker is now sleeping for seconds at a time
        job_id = await _make_job(client, "k")
        await publish_pending(pool, redis)
        started = time.monotonic()
        gate.touch()  # POST /goals does this
        await _wait_for(
            lambda: pool.fetchval(
                "SELECT status = 'succeeded' FROM jobs WHERE id = $1::uuid", job_id
            ),
            timeout=3,
        )
        assert time.monotonic() - started < 2
    finally:
        await workers.stop()


# --- quiet background loops ----------------------------------------------------------------------


async def test_retry_mover_makes_no_redis_calls_while_nothing_is_delayed(
    client, pool, counting_redis
):
    gate = SleepGate(enabled=True, idle_seconds=3600)
    stop = asyncio.Event()
    mover = asyncio.create_task(run_delayed_mover(pool, counting_redis, stop, 0.05, gate))
    await asyncio.sleep(0.2)
    start = counting_redis.command_count
    await asyncio.sleep(1.0)
    assert counting_redis.command_count - start == 0  # without the gate: ~20 polls a second

    # A retry is scheduled: the mover wakes, moves it once it is due, and goes quiet again.
    job_id = await _make_job(client, "k")
    await pool.execute("UPDATE jobs SET status = 'retrying' WHERE id = $1::uuid", job_id)
    await counting_redis.zadd("jobs:delayed", {job_id: time.time()})
    gate.delayed.set()
    await _wait_for(lambda: _stream_len(counting_redis, 1), timeout=3)
    stop.set()
    await asyncio.wait_for(mover, 3)


async def _stream_len(redis, n) -> bool:
    return await redis.xlen(STREAM) == n


async def test_idle_hub_reads_nothing_until_a_client_connects(counting_redis):
    hub = EventHub(counting_redis, block_ms=100, idle_mode=True)
    await hub.init()
    stop = asyncio.Event()
    task = asyncio.create_task(hub.run(stop))
    await asyncio.sleep(0.3)
    start = counting_redis.command_count
    await asyncio.sleep(0.6)
    assert counting_redis.command_count == start  # nobody connected: no XREAD at all

    received = []

    async def send(message):
        received.append(message)

    await hub.ensure_active()
    client = hub.register(send, None)
    client.start()
    await counting_redis.xadd(EVENTS_STREAM, {"id": "1", "type": "t", "payload": "{}", "ts": "now"})
    await _wait_for(lambda: _got(received))
    await hub.unregister(client)
    stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _got(received) -> bool:
    return bool(received)


# --- reconcile after Redis loses data ------------------------------------------------------------


async def test_reconcile_requeues_jobs_after_a_redis_flush(client, pool, redis):
    goal_id = (await client.post("/goals", json={"prompt": "survive a flush"})).json()["id"]
    await publish_pending(pool, redis)
    await publish_events(pool, redis)
    assert await redis.xlen(STREAM) == 1

    await redis.flushall()  # Redis loses everything: the job message and the events
    assert await redis.xlen(STREAM) == 0

    result = await reconcile(pool, redis)
    assert result["jobs"] == 1 and result["events"] >= 1
    assert (await reconcile(pool, redis))["jobs"] == 0  # the first run's outbox row is waiting
    assert await pool.fetchval("SELECT count(*) FROM outbox WHERE published_at IS NULL") == 1
    assert await pool.fetchval("SELECT count(*) FROM events WHERE type = 'job.reconciled'") == 1

    # The normal pipeline now finishes the job: dispatcher -> stream -> worker -> plan stored.
    await publish_pending(pool, redis)
    await publish_events(pool, redis)
    assert await redis.xlen(EVENTS_STREAM) >= 1  # clients get history again
    workers = WorkerPool(pool, redis, CountingMock(), size=1, block_ms=50)
    await workers.start()
    try:
        await _wait_for(
            lambda: pool.fetchval(
                "SELECT status <> 'planning' FROM goals WHERE id = $1::uuid", goal_id
            )
        )
    finally:
        await workers.stop()


async def test_reconcile_leaves_present_finished_and_already_queued_jobs_alone(client, pool, redis):
    kept = await _make_job(client, "kept")
    lost = await _make_job(client, "lost")
    done = await _make_job(client, "done")
    await pool.execute("UPDATE jobs SET status = 'succeeded' WHERE id = $1::uuid", done)
    await publish_pending(pool, redis)
    await redis.flushall()
    # `kept` is back in the stream; `lost` is not; `done` is finished and must be ignored.
    await redis.xadd(STREAM, {"job_id": kept, "type": "llm.mock", "payload": "{}"})
    assert (await reconcile(pool, redis))["jobs"] == 1
    reconciled = await pool.fetch(
        "SELECT payload->>'job_id' AS j FROM events WHERE type = 'job.reconciled'"
    )
    assert [r["j"] for r in reconciled] == [lost]
    assert (await reconcile(pool, redis))["jobs"] == 0  # already waiting in the outbox


async def test_reconcile_finds_retries_lost_from_the_delayed_set(client, pool, redis):
    job_id = await _make_job(client, "k")
    await pool.execute("UPDATE jobs SET status = 'retrying' WHERE id = $1::uuid", job_id)
    await redis.zadd("jobs:delayed", {job_id: time.time() + 60})
    await publish_pending(pool, redis)
    assert (await reconcile(pool, redis))["jobs"] == 0  # present in the delayed set
    await redis.flushall()
    assert (await reconcile(pool, redis))["jobs"] == 1


async def test_workers_recreate_a_lost_consumer_group_and_carry_on(client, pool, redis):
    workers = WorkerPool(pool, redis, CountingMock(), size=1, block_ms=50)
    await workers.start()
    try:
        await asyncio.sleep(0.2)
        await redis.flushall()  # stream and group vanish under the running workers
        job_id = await _make_job(client, "k")
        await publish_pending(pool, redis)  # XADD recreates the stream, but not the group
        await _wait_for(
            lambda: pool.fetchval(
                "SELECT status = 'succeeded' FROM jobs WHERE id = $1::uuid", job_id
            ),
            timeout=6,
        )
        assert (await redis.xpending(STREAM, GROUP))["pending"] == 0
    finally:
        await workers.stop()


async def test_reconcile_after_flush_keeps_a_planned_goal_progressing(client, pool, redis):
    """A running goal whose task job was lost still finishes after reconcile."""
    from uuid import UUID

    goal_id = (await client.post("/goals", json={"prompt": "g"})).json()["id"]
    async with pool.acquire() as conn, conn.transaction():
        plan = (await MockPlanner().plan("g")).plan
        await pool.execute("UPDATE jobs SET status = 'succeeded' WHERE type = 'planner'")
        await store_plan(
            conn, UUID(goal_id), plan, "mock"
        )  # t1 is queued, its job is in the outbox
    await publish_pending(pool, redis)
    await redis.flushall()
    assert (await reconcile(pool, redis))["jobs"] == 1
    await publish_pending(pool, redis)
    task_payload = json.loads(await pool.fetchval("SELECT payload FROM jobs WHERE type = 'task'"))
    assert task_payload["goal_id"] == goal_id
    assert await redis.xlen(STREAM) == 1


async def test_command_budget_does_not_wake_an_idle_system(counting_redis):
    from app.redis_budget import CommandBudget

    budget = CommandBudget(counting_redis, limit=1000, guard_ratio=0.9)
    await budget.flush()
    stop = asyncio.Event()
    task = asyncio.create_task(budget.run(stop, interval=0.05, min_delta=20))
    await asyncio.sleep(0.2)
    start = counting_redis.command_count
    await asyncio.sleep(0.5)
    assert counting_redis.command_count == start  # nothing to report, so no INCRBY
    for _ in range(25):
        await counting_redis.ping()
    await _wait_for(lambda: _flushed(budget, 25))
    stop.set()
    await task


async def _flushed(budget, n) -> bool:
    return budget.used >= n
