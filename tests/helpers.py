"""Shared test helpers (imported by the integration tests)."""

import asyncio
import time

from app.dispatcher import STREAM
from app.providers.mock_llm import MockLLM
from app.worker import GROUP


async def _make_job(client, key: str, payload: dict | None = None, max_attempts: int = 5) -> str:
    r = await client.post(
        "/jobs",
        json={
            "type": "llm.mock",
            "payload": payload or {"prompt": "hi"},
            "max_attempts": max_attempts,
        },
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
