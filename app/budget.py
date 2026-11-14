"""Daily cap on real Gemini calls: one Redis counter per UTC day.

Shared by the planner now and by capped worker calls later (see PLAN.md). Over the cap the caller
falls back to the mock provider, so the demo keeps working without spending quota.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from redis.asyncio import Redis

KEY_PREFIX = "gemini:calls:"
TTL_SECONDS = 2 * 24 * 3600  # outlive the day it counts, then clean itself up


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class DailyCap:
    def __init__(self, redis: Redis, limit: int, today: Callable[[], str] = _today):
        self.redis, self.limit, self.today = redis, limit, today

    async def acquire(self) -> bool:
        """Reserve one call. INCR is atomic, so concurrent callers never exceed the limit.

        A refused call still increments the counter; that only makes it read slightly above the
        limit, which is harmless and avoids a second round trip.
        """
        key = KEY_PREFIX + self.today()
        used = await self.redis.incr(key)
        if used == 1:
            await self.redis.expire(key, TTL_SECONDS)
        return used <= self.limit
