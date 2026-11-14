import json

import pytest

from app.dispatcher import STREAM, publish_pending


async def _make_job(client, key: str, payload: dict | None = None) -> str:
    r = await client.post(
        "/jobs",
        json={"type": "llm.mock", "payload": payload or {}},
        headers={"Idempotency-Key": key},
    )
    return r.json()["id"]


async def test_publishes_outbox_rows_to_stream_and_marks_them(client, pool, redis):
    job_id = await _make_job(client, "k1", {"prompt": "hi"})

    assert await publish_pending(pool, redis) == 1

    entries = await redis.xrange(STREAM)
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["job_id"] == job_id
    assert fields["type"] == "llm.mock"
    assert json.loads(fields["payload"]) == {"prompt": "hi"}
    assert await pool.fetchval("SELECT count(*) FROM outbox WHERE published_at IS NULL") == 0


async def test_does_not_publish_the_same_row_twice(client, pool, redis):
    await _make_job(client, "k1")
    assert await publish_pending(pool, redis) == 1
    assert await publish_pending(pool, redis) == 0
    assert await redis.xlen(STREAM) == 1


async def test_respects_batch_size_and_order(client, pool, redis):
    ids = [await _make_job(client, f"k{i}") for i in range(5)]

    assert await publish_pending(pool, redis, batch_size=3) == 3
    assert await publish_pending(pool, redis, batch_size=3) == 2

    published = [fields["job_id"] for _, fields in await redis.xrange(STREAM)]
    assert published == ids


async def test_job_is_not_lost_when_redis_fails(client, pool, redis):
    job_id = await _make_job(client, "k1")

    class BrokenRedis:
        async def xadd(self, *args, **kwargs):
            raise ConnectionError("redis down")

    with pytest.raises(ConnectionError):
        await publish_pending(pool, BrokenRedis())

    # Nothing was marked published, so the job is still waiting in the outbox.
    assert await pool.fetchval("SELECT count(*) FROM outbox WHERE published_at IS NULL") == 1
    assert await redis.xlen(STREAM) == 0

    # Redis comes back: the same job is delivered.
    assert await publish_pending(pool, redis) == 1
    assert (await redis.xrange(STREAM))[0][1]["job_id"] == job_id
