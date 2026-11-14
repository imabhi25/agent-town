import os

import asyncpg
import pytest
import redis.asyncio as aioredis

from app.config import settings
from app.dispatcher import STREAM
from app.retry import DELAYED_KEY, DLQ_STREAM

TRUNCATE_ALL = "TRUNCATE goals, tasks, jobs, job_outputs, outbox, events CASCADE"


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
    await r.delete(STREAM, DLQ_STREAM, DELAYED_KEY)
    yield r
    await r.delete(STREAM, DLQ_STREAM, DELAYED_KEY)
    await r.aclose()


@pytest.fixture
async def client(pool):
    import httpx

    from app.main import app

    app.state.pool = pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
