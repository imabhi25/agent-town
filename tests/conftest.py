import os

import asyncpg
import pytest
import redis.asyncio as aioredis

from app.config import settings
from app.dispatcher import STREAM
from app.event_publisher import EVENTS_STREAM
from app.retry import DELAYED_KEY, DLQ_STREAM


@pytest.fixture(autouse=True)
def _free_text_planner(monkeypatch):
    """The public default is PLANNER=fixed (presets only). Most tests submit free-text goals, so
    they run with the "gemini" planner (mock planner here: no key). Tests of the fixed planner set
    settings.planner = "fixed" themselves."""
    monkeypatch.setattr(settings, "planner", "gemini")


TRUNCATE_ALL = "TRUNCATE goals, tasks, jobs, job_outputs, outbox, events CASCADE"


async def _clean_redis(r) -> None:
    await r.delete(STREAM, DLQ_STREAM, DELAYED_KEY, EVENTS_STREAM)
    # Heartbeat keys and the daily Gemini counters must not leak between tests.
    for pattern in (
        "worker:hb:*",
        "gemini:calls:*",
        "ratelimit:*",
        "circuit:*",
        "goals:live:*",
        "budget:redis:*",
    ):
        keys = [k async for k in r.scan_iter(match=pattern)]
        if keys:
            await r.delete(*keys)


def _unavailable(reason: str):
    # In CI the services must exist, so a connection failure is a real failure there.
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture
async def pool():
    try:
        p = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    except (OSError, asyncpg.PostgresError) as e:
        _unavailable(f"Postgres not available: {e}")
    await p.execute(TRUNCATE_ALL)
    yield p
    # Clean up after the test too, so a finished run leaves no rows behind.
    await p.execute(TRUNCATE_ALL)
    await p.close()


@pytest.fixture
async def redis():
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.ping()
    except OSError as e:
        await r.aclose()
        _unavailable(f"Redis not available: {e}")
    await _clean_redis(r)
    yield r
    await _clean_redis(r)
    await r.aclose()


@pytest.fixture
async def counting_redis():
    from app.redis_budget import CountingRedis

    r = CountingRedis.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.ping()
    except OSError as e:
        await r.aclose()
        _unavailable(f"Redis not available: {e}")
    await _clean_redis(r)
    yield r
    await _clean_redis(r)
    await r.aclose()


@pytest.fixture
async def client(pool):
    import httpx

    from app.main import app

    app.state.pool = pool
    # `app` is a module-level singleton: clear what an earlier test (or lifespan) attached.
    for name in ("redis", "guard", "gate", "hub", "workers"):
        if hasattr(app.state, name):
            delattr(app.state, name)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
