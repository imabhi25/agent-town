"""Step 4: rate limits, circuit breaker, provider limiter, mock_* fields, daily caps."""

import asyncio

import pytest
from helpers import CountingMock, _make_job, _wait_for

from app.budget import DailyCap
from app.circuit import CircuitBreaker
from app.dispatcher import publish_pending
from app.jobs import MAX_MOCK_LATENCY
from app.protection import GoalGuard, Protection
from app.providers.gemini_llm import RoutingProvider
from app.providers.mock_llm import MockLLM
from app.ratelimit import TokenBucket
from app.redis_budget import CommandBudget
from app.worker import WorkerPool

# --- /healthz --------------------------------------------------------------------------------


async def test_healthz_is_instant_and_touches_neither_postgres_nor_redis(client):
    from app.main import app

    class Explodes:
        def __getattr__(self, name):
            raise AssertionError(f"healthz touched {name}")

    app.state.pool = Explodes()
    app.state.redis = Explodes()
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "live": True, "sleeping": False}


# --- Lua token bucket --------------------------------------------------------------------------


async def test_bucket_allows_a_burst_then_refuses_with_a_wait(redis):
    bucket = TokenBucket(redis)
    results = [await bucket.take("ratelimit:t", 3, 0.001) for _ in range(4)]
    assert [ok for ok, _ in results] == [True, True, True, False]
    assert results[3][1] > 100  # ~1000s until the next token at 0.001/s


async def test_bucket_refills_over_time(redis):
    bucket = TokenBucket(redis)
    assert (await bucket.take("ratelimit:t", 1, 20))[0]
    assert not (await bucket.take("ratelimit:t", 1, 20))[0]
    await asyncio.sleep(0.12)  # 20 tokens/s -> back to one token
    assert (await bucket.take("ratelimit:t", 1, 20))[0]


async def test_bucket_keys_are_independent(redis):
    bucket = TokenBucket(redis)
    assert (await bucket.take("ratelimit:a", 1, 0.001))[0]
    assert not (await bucket.take("ratelimit:a", 1, 0.001))[0]
    assert (await bucket.take("ratelimit:b", 1, 0.001))[0]


async def test_bucket_is_atomic_under_concurrency(counting_redis):
    """300 simultaneous callers (from several 'processes') share a bucket of 25: exactly 25 win."""
    buckets = [TokenBucket(counting_redis) for _ in range(5)]
    results = await asyncio.gather(
        *(buckets[i % 5].take("ratelimit:race", 25, 0.0001) for i in range(300))
    )
    assert sum(ok for ok, _ in results) == 25


# --- POST /goals guards ------------------------------------------------------------------------


@pytest.fixture
async def guarded(client, redis):
    from app.main import app

    app.state.redis = redis

    def install(**kwargs) -> None:
        kwargs.setdefault("daily_cap", 0)
        app.state.guard = GoalGuard(
            redis, kwargs.pop("budget", None), kwargs.pop("gate", None), **kwargs
        )

    return client, install


async def _goal(client, ip: str | None = None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return await client.post("/goals", json={"prompt": "x"}, headers=headers)


async def test_goals_are_rate_limited_per_ip(guarded):
    client, install = guarded
    install(capacity=2, refill_per_sec=0.001, trust_forwarded_for=True)
    assert [(await _goal(client, "1.1.1.1")).status_code for _ in range(2)] == [201, 201]
    third = await _goal(client, "1.1.1.1")
    assert third.status_code == 429
    assert int(third.headers["retry-after"]) >= 1 and "Too many goals" in third.json()["detail"]
    assert (await _goal(client, "2.2.2.2")).status_code == 201  # another address is unaffected


async def test_forwarded_for_is_ignored_unless_trusted(guarded):
    client, install = guarded
    install(capacity=1, refill_per_sec=0.001, trust_forwarded_for=False)
    assert (await _goal(client, "1.1.1.1")).status_code == 201
    assert (await _goal(client, "9.9.9.9")).status_code == 429  # same real client address


async def test_daily_goal_cap_returns_429_with_a_clear_message(guarded):
    client, install = guarded
    install(capacity=100, refill_per_sec=100, daily_cap=2)
    assert [(await _goal(client)).status_code for _ in range(2)] == [201, 201]
    r = await _goal(client)
    assert r.status_code == 429
    assert "daily limit of 2 goals" in r.json()["detail"] and "recorded demo" in r.json()["detail"]


async def test_command_budget_guard_turns_live_off(guarded, counting_redis):
    client, install = guarded
    budget = CommandBudget(counting_redis, limit=100, guard_ratio=0.9)
    install(capacity=100, refill_per_sec=100, budget=budget)
    await budget.flush()
    assert (await _goal(client)).status_code == 201
    await counting_redis.set(budget.key(), 95)  # the month's usage nears the limit
    await budget.flush()
    assert not budget.live_available
    r = await _goal(client)
    assert r.status_code == 503 and "unavailable" in r.json()["detail"]
    health = (await client.get("/healthz")).json()
    assert health["status"] == "ok" and health["live"] is False  # backend up, live mode closed


async def test_command_counter_accumulates_into_the_monthly_key(counting_redis):
    budget = CommandBudget(counting_redis, limit=1000, guard_ratio=0.9, month=lambda: "2026-05")
    await budget.flush()
    before = budget.used
    for _ in range(10):
        await counting_redis.ping()
    total = await budget.flush()
    assert total - before >= 10 and int(await counting_redis.get("budget:redis:2026-05")) == total
    await counting_redis.delete("budget:redis:2026-05")


# --- mock_* fields -----------------------------------------------------------------------------


async def _post_job(client, payload, key="k"):
    return await client.post(
        "/jobs", json={"type": "llm.mock", "payload": payload}, headers={"Idempotency-Key": key}
    )


async def test_public_jobs_reject_mock_fields_and_clamp_latency(client, pool):
    assert (await _post_job(client, {"prompt": "x", "mock_mode": "500"}, "a")).status_code == 400
    assert (
        await _post_job(client, {"prompt": "x", "mock_failure_rate": 1}, "b")
    ).status_code == 400
    assert (
        await _post_job(client, {"prompt": "x", "mock_latency": "slow"}, "c")
    ).status_code == 400
    r = await _post_job(client, {"prompt": "x", "mock_latency": 9999}, "d")
    assert r.status_code == 201
    stored = await pool.fetchval("SELECT payload->>'mock_latency' FROM jobs")
    assert float(stored) == MAX_MOCK_LATENCY
    assert (await _post_job(client, {"prompt": "plain"}, "e")).status_code == 201


async def test_trusted_callers_may_set_mock_mode_but_latency_is_still_clamped(client, pool):
    await _make_job(client, "t", {"prompt": "x", "mock_mode": "500", "mock_latency": 500})
    row = await pool.fetchrow("SELECT payload FROM jobs")
    import json

    payload = json.loads(row["payload"])
    assert payload["mock_mode"] == "500" and payload["mock_latency"] == MAX_MOCK_LATENCY


async def test_worker_clamps_latency_even_if_a_payload_slips_through(client, pool, redis):
    seen = []

    class Spy(MockLLM):
        async def complete(self, prompt, *, mode=None, latency=None):
            seen.append(latency)
            return {"text": "ok"}

    job_id = await _make_job(client, "k")
    await pool.execute('UPDATE jobs SET payload = \'{"prompt": "x", "mock_latency": 500}\'')
    await publish_pending(pool, redis)
    workers = WorkerPool(pool, redis, Spy(), size=1, block_ms=50)
    await workers.start()
    try:
        await _wait_for(
            lambda: pool.fetchval(
                "SELECT status = 'succeeded' FROM jobs WHERE id = $1::uuid", job_id
            )
        )
    finally:
        await workers.stop()
    assert seen == [MAX_MOCK_LATENCY]


# --- circuit breaker ---------------------------------------------------------------------------


def _breaker(redis, threshold=3, open_s=0.3, probe_s=0.3) -> CircuitBreaker:
    return CircuitBreaker(redis, threshold, open_s, probe_s)


async def test_circuit_opens_after_consecutive_failures_only(redis):
    cb = _breaker(redis)
    assert [await cb.record("p", False) for _ in range(2)] == ["", ""]
    assert await cb.record("p", True) == ""  # a success resets the count
    assert [await cb.record("p", False) for _ in range(3)] == ["", "", "opened"]
    state, wait = await cb.allow("p")
    assert state == "open" and 0 < wait <= 0.3
    assert (await cb.allow("other"))[0] == "closed"  # per provider


async def test_half_open_allows_exactly_one_probe_and_success_closes(redis):
    cb = _breaker(redis)
    for _ in range(3):
        await cb.record("p", False)
    await asyncio.sleep(0.35)
    states = [s for s, _ in await asyncio.gather(*(cb.allow("p") for _ in range(10)))]
    assert states.count("probe") == 1 and states.count("open") == 9
    assert await cb.state("p") == "half_open"
    assert await cb.record("p", True) == "closed"
    assert (await cb.allow("p"))[0] == "closed"


async def test_failed_probe_reopens_the_circuit(redis):
    cb = _breaker(redis)
    for _ in range(3):
        await cb.record("p", False)
    await asyncio.sleep(0.35)
    assert (await cb.allow("p"))[0] == "probe"
    assert await cb.record("p", False) == "opened"
    assert (await cb.allow("p"))[0] == "open"


async def test_a_probe_that_never_reports_expires(redis):
    cb = _breaker(redis)
    for _ in range(3):
        await cb.record("p", False)
    await asyncio.sleep(0.35)
    assert (await cb.allow("p"))[0] == "probe"
    assert (await cb.allow("p"))[0] == "open"  # probe in flight
    await asyncio.sleep(0.35)  # the prober died; after probe_timeout someone else may try
    assert (await cb.allow("p"))[0] == "probe"


async def test_transitions_emit_circuit_events(pool, redis):
    protection = Protection(pool, redis, threshold=2, open_seconds=0.2, probe_timeout=0.2)
    await protection.record("llm", False)
    await protection.record("llm", False)
    await asyncio.sleep(0.25)
    assert (await protection.admit("llm")).ok  # the probe
    await protection.record("llm", True)
    types = [r["type"] for r in await pool.fetch("SELECT type FROM events ORDER BY id")]
    assert types == ["circuit.opened", "circuit.closed"]


async def test_open_circuit_delays_jobs_without_burning_attempts(client, pool, redis):
    protection = Protection(pool, redis, threshold=2, open_seconds=1.0, probe_timeout=1.0)
    await protection.record("llm", False)
    await protection.record("llm", False)  # circuit is now open
    ids = [await _make_job(client, f"k{i}") for i in range(3)]
    await publish_pending(pool, redis)

    provider = CountingMock()
    workers = WorkerPool(
        pool, redis, provider, size=2, block_ms=50, mover_interval=0.1, protection=protection
    )
    await workers.start()
    try:
        await asyncio.sleep(0.5)
        # Nothing ran and nothing was charged: jobs are parked, attempts are still 0.
        assert provider.calls == 0
        rows = await pool.fetch("SELECT status, attempts FROM jobs")
        assert [(r["status"], r["attempts"]) for r in rows] == [("queued", 0)] * 3
        assert await redis.zcard("jobs:delayed") == 3

        # After the open period one probe goes through; it succeeds, the circuit closes, the rest follow.
        await _wait_for(
            lambda: pool.fetchval("SELECT count(*) = 3 FROM jobs WHERE status = 'succeeded'"),
            timeout=8,
        )
    finally:
        await workers.stop()
    assert all(r["attempts"] == 1 for r in await pool.fetch("SELECT attempts FROM jobs"))
    types = [
        r["type"]
        for r in await pool.fetch("SELECT type FROM events WHERE type LIKE 'circuit.%' ORDER BY id")
    ]
    assert types == ["circuit.opened", "circuit.closed"]
    assert ids


async def test_provider_rate_limit_parks_excess_jobs_without_burning_attempts(client, pool, redis):
    protection = Protection(pool, redis, rate_per_sec=0.5, burst=2)
    for i in range(8):
        await _make_job(client, f"k{i}")
    await publish_pending(pool, redis)
    provider = CountingMock()
    workers = WorkerPool(
        pool, redis, provider, size=3, block_ms=50, mover_interval=0.1, protection=protection
    )
    await workers.start()
    try:
        await _wait_for(
            lambda: pool.fetchval("SELECT count(*) = 2 FROM jobs WHERE status = 'succeeded'")
        )
        await asyncio.sleep(0.4)
    finally:
        await workers.stop()
    assert provider.calls == 2  # only the burst reached the provider
    waiting = await pool.fetch("SELECT attempts FROM jobs WHERE status = 'queued'")
    assert len(waiting) == 6 and all(r["attempts"] == 0 for r in waiting)


# --- Gemini routing and the daily cap ------------------------------------------------------------


class FakeGemini:
    def __init__(self):
        self.calls = 0
        self.aio = type("A", (), {"models": self})()

    async def generate_content(self, **kwargs):
        self.calls += 1
        return type("R", (), {"text": "real answer"})()


async def test_routing_provider_uses_gemini_only_for_real_requests_under_the_cap(redis):
    client = FakeGemini()
    cap = DailyCap(redis, limit=2, today=lambda: "2026-03-01")
    provider = RoutingProvider(MockLLM(min_latency=0, max_latency=0), client, "m", cap)
    answers = [(await provider.complete("p", real=True))["text"] for _ in range(3)]
    assert answers[:2] == ["real answer", "real answer"]
    assert answers[2].startswith("[mock]")  # over the cap -> mock
    assert client.calls == 2
    assert (await provider.complete("p"))["text"].startswith("[mock]")  # not a real request
    assert client.calls == 2  # mock jobs and chaos never spend the quota


# --- live demo profile ---------------------------------------------------------------------------


def test_live_demo_profile_values():
    from app.config import Settings

    s = Settings(live_demo=True)
    assert (s.worker_count, s.heartbeat_interval, s.heartbeat_ttl, s.lease_seconds) == (
        2,
        10,
        30,
        45,
    )
    assert s.worker_block_ms == 5000 and s.embedded_workers
    assert s.heartbeat_ttl < s.lease_seconds
    normal = Settings()
    assert not normal.live_demo and normal.worker_count == 4 and normal.worker_block_ms == 500
