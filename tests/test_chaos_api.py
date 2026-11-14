"""Chaos endpoints, the synthetic load generator and /stats (mock jobs only)."""

import asyncio

import pytest
from helpers import CountingMock, _wait_for

from app.config import Settings, settings
from app.dispatcher import run_dispatcher
from app.loadgen import run_load_generator
from app.protection import GoalGuard
from app.stats import compute_stats
from app.worker import WorkerPool


@pytest.fixture
async def chaos(client, pool, redis):
    """The API with a real worker pool inside it (what EMBEDDED_WORKERS does) and a dispatcher."""
    from app.main import app

    app.state.redis = redis
    provider = CountingMock()
    workers = WorkerPool(pool, redis, provider, size=3, block_ms=50, mover_interval=0.1)
    app.state.workers = workers
    await workers.start()
    stop = asyncio.Event()
    dispatcher = asyncio.create_task(run_dispatcher(pool, redis, stop, 0.05))
    yield client, workers, provider
    stop.set()
    await dispatcher
    await workers.stop()


async def _events(pool, type_):
    return await pool.fetch("SELECT payload FROM events WHERE type = $1 ORDER BY id", type_)


# --- kill / start --------------------------------------------------------------------------------


async def test_kill_a_named_worker(chaos, pool, redis):
    client, workers, _ = chaos
    r = await client.post("/chaos/kill", json={"worker_id": "worker-2"})
    assert r.status_code == 200 and r.json()["killed"] == "worker-2"
    assert sorted(workers.tasks) == ["worker-1", "worker-3"]
    assert len(await _events(pool, "worker.killed")) == 1
    # A crash, not a shutdown: the heartbeat key is left to expire on its own.
    assert await redis.exists("worker:hb:worker-2")


async def test_kill_random_worker_and_unknown_or_none_left(chaos):
    client, workers, _ = chaos
    killed = (await client.post("/chaos/kill", json={})).json()["killed"]
    assert killed not in workers.tasks and len(workers.tasks) == 2
    assert (await client.post("/chaos/kill", json={"worker_id": "nope"})).status_code == 404
    await client.post("/chaos/kill", json={})
    await client.post("/chaos/kill", json={})
    assert (await client.post("/chaos/kill", json={})).status_code == 404  # none left


async def test_start_replacement_workers(chaos, pool):
    client, workers, _ = chaos
    await client.post("/chaos/kill", json={"worker_id": "worker-1"})
    r = await client.post("/chaos/start", json={})
    assert r.status_code == 200 and r.json()["started"] == "worker-4"  # ids are not reused
    named = await client.post("/chaos/start", json={"worker_id": "worker-1"})
    assert named.json()["started"] == "worker-1"
    assert (await client.post("/chaos/start", json={"worker_id": "worker-1"})).status_code == 409
    assert sorted(workers.tasks) == ["worker-1", "worker-2", "worker-3", "worker-4"]
    assert len(await _events(pool, "worker.started")) == 2


async def test_chaos_needs_workers_inside_the_api(client):
    assert (await client.post("/chaos/kill", json={})).status_code == 409
    assert (await client.post("/chaos/flood", json={"count": 1})).status_code == 409


# --- mock config and flood -----------------------------------------------------------------------


async def test_failure_rate_and_latency_are_applied_and_clamped(chaos):
    client, _, provider = chaos
    r = await client.post(
        "/chaos/mock", json={"failure_rate": 0.4, "min_latency": 0.1, "max_latency": 99}
    )
    assert r.json() == {"failure_rate": 0.4, "min_latency": 0.1, "max_latency": 10.0}
    assert (provider.failure_rate, provider.max_latency) == (0.4, 10.0)
    assert (await client.post("/chaos/mock", json={"failure_rate": 1.5})).status_code == 422
    bad = await client.post("/chaos/mock", json={"min_latency": 5, "max_latency": 1})
    assert bad.status_code == 400
    assert (await client.get("/chaos/status")).json()["failure_rate"] == 0.4


async def test_failure_rate_makes_jobs_retry(chaos, pool):
    client, _, _ = chaos
    await client.post("/chaos/mock", json={"failure_rate": 1.0, "min_latency": 0, "max_latency": 0})
    await client.post("/chaos/flood", json={"count": 2})
    await _wait_for(lambda: _count(pool, "job.retry_scheduled", 2), timeout=5)


async def _count(pool, type_, n) -> bool:
    return len(await _events(pool, type_)) >= n


async def test_flood_creates_mock_jobs_that_all_complete(chaos, pool):
    client, _, _provider = chaos
    r = await client.post("/chaos/flood", json={"count": 25, "latency": 0})
    assert r.json() == {"created": 25}
    await _wait_for(
        lambda: pool.fetchval("SELECT count(*) = 25 FROM jobs WHERE status = 'succeeded'"),
        timeout=8,
    )
    assert await pool.fetchval("SELECT count(DISTINCT type) FROM jobs") == 1  # llm.mock only
    assert (await _events(pool, "chaos.flood"))[0]["payload"]
    await client.post("/chaos/flood", json={"count": 5, "latency": 0})  # unique keys: a new batch
    await _wait_for(lambda: pool.fetchval("SELECT count(*) = 30 FROM jobs"))


async def test_flood_limits(chaos, monkeypatch):
    client, _, _ = chaos
    assert (await client.post("/chaos/flood", json={"count": 0})).status_code == 422
    monkeypatch.setattr(settings, "flood_max", 100)  # the live demo's limit
    over = await client.post("/chaos/flood", json={"count": 101})
    assert over.status_code == 400 and "100" in over.json()["detail"]
    assert (await client.post("/chaos/flood", json={"count": 100, "latency": 0})).status_code == 200


def test_flood_limit_is_2000_locally_and_100_in_the_live_demo():
    assert Settings().flood_max == 2000
    assert Settings(live_demo=True).flood_max == 100
    assert Settings(live_demo=True, flood_max=5000).flood_max == 100


async def test_chaos_endpoints_are_rate_limited_per_ip(chaos, redis, monkeypatch):
    client, _, _ = chaos
    from app.main import app

    monkeypatch.setattr(settings, "chaos_rate_capacity", 2)
    monkeypatch.setattr(settings, "chaos_rate_per_sec", 0.001)
    app.state.guard = GoalGuard(redis, None, None, daily_cap=0)
    assert (await client.get("/chaos/status")).status_code == 200
    assert (await client.get("/chaos/status")).status_code == 200
    third = await client.get("/chaos/status")
    assert third.status_code == 429 and "Retry-After" in third.headers


# --- synthetic load generator --------------------------------------------------------------------


async def test_load_generator_creates_mock_jobs_until_stopped(pool):
    stop = asyncio.Event()
    task = asyncio.create_task(run_load_generator(pool, stop, interval=0.05))
    await _wait_for(lambda: _jobs_at_least(pool, 5))
    stop.set()
    await asyncio.wait_for(task, 2)
    total = await pool.fetchval("SELECT count(*) FROM jobs")
    assert await pool.fetchval("SELECT count(*) FROM jobs WHERE type = 'llm.mock'") == total
    assert await pool.fetchval("SELECT count(*) FROM outbox") == total
    await asyncio.sleep(0.2)
    assert await pool.fetchval("SELECT count(*) FROM jobs") == total  # really stopped


async def _jobs_at_least(pool, n) -> bool:
    return await pool.fetchval("SELECT count(*) FROM jobs") >= n


def test_load_generator_is_off_by_default_and_forced_off_in_the_live_demo():
    assert Settings().load_generator is False
    assert Settings(load_generator=True).load_generator is True
    assert Settings(live_demo=True, load_generator=True).load_generator is False


# --- stats ---------------------------------------------------------------------------------------


async def _insert_done(pool, created_ago: float, took_ms: int, status="succeeded"):
    await pool.execute(
        """
        INSERT INTO jobs (type, payload, status, created_at, completed_at)
        VALUES ('llm.mock', '{}', $1, now() - make_interval(secs => $2),
                now() - make_interval(secs => $2) + make_interval(secs => $3))
        """,
        status,
        created_ago,
        took_ms / 1000,
    )


async def test_stats_math(pool, redis):
    # Ten successes that took 100..1000 ms and completed within the last minute.
    for k in range(1, 11):
        await _insert_done(pool, created_ago=20, took_ms=k * 100)
    await _insert_done(pool, created_ago=3000, took_ms=5000)  # old: outside every window
    await _insert_done(pool, created_ago=10, took_ms=50, status="dead")
    await _insert_done(pool, created_ago=10, took_ms=50, status="dead")
    for status in ("queued", "queued", "queued", "queued", "retrying", "running"):
        await pool.execute("INSERT INTO jobs (type, status) VALUES ('llm.mock', $1)", status)
    for _ in range(3):
        await pool.execute("INSERT INTO events (type) VALUES ('job.retry_scheduled')")
    await redis.set("worker:hb:worker-1", "1", ex=30)
    await redis.set("worker:hb:worker-2", "1", ex=30)

    stats = await compute_stats(pool, redis)
    latency = stats["latency_ms"]
    # Sorted 100..1000: p50 = 550, p95 = 900 + 0.55*100 = 955, p99 = 900 + 0.91*100 = 991.
    assert latency["p50"] == pytest.approx(550, abs=1)
    assert latency["p95"] == pytest.approx(955, abs=1)
    assert latency["p99"] == pytest.approx(991, abs=1)
    assert latency["samples"] == 10  # the 50-minute-old job is outside the window
    assert stats["completed_last_minute"] == 10
    assert stats["throughput_per_sec"] == pytest.approx(10 / 60, abs=0.001)
    assert (stats["succeeded"], stats["dead"], stats["retries"]) == (11, 2, 3)
    assert stats["queue_depth"] == 5  # 4 queued + 1 retrying
    assert stats["running"] == 1 and stats["active_workers"] == 2


async def test_stats_when_nothing_has_happened(pool, redis):
    stats = await compute_stats(pool, redis)
    assert stats["latency_ms"]["p50"] is None and stats["latency_ms"]["samples"] == 0
    assert stats["throughput_per_sec"] == 0 and stats["active_workers"] == 0
    assert (stats["succeeded"], stats["retries"], stats["dead"], stats["queue_depth"]) == (
        0,
        0,
        0,
        0,
    )


async def test_stats_endpoint_and_snapshot_include_stats(client, pool, redis):
    from app.main import app

    app.state.redis = redis
    await _insert_done(pool, created_ago=5, took_ms=200)
    body = (await client.get("/stats")).json()
    assert body["succeeded"] == 1 and body["latency_ms"]["p50"] == pytest.approx(200, abs=1)
    snapshot = (await client.get("/state")).json()
    assert snapshot["stats"]["succeeded"] == 1
    assert set(snapshot["stats"]) == set(body)


async def test_stopping_a_pool_whose_workers_were_all_killed_is_safe(pool, redis):
    workers = WorkerPool(pool, redis, CountingMock(), size=2, block_ms=50)
    await workers.start()
    for wid in list(workers.tasks):
        await workers.kill_worker(wid)
    await workers.stop()  # used to raise: DEL with no keys
