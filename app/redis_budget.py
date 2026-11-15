"""Redis command budget for the free tier (Upstash free = 500K commands/month).

CountingRedis counts every command this process sends. CommandBudget adds the count to a Redis
key for the current UTC month (so it survives restarts and sums across processes) and says whether
live mode is still available: past `guard_ratio` of the limit the API reports "live unavailable"
and the frontend falls back to the recorded demo instead of running the account out of commands.
The count is approximate (our own commands only; blocked reads count once per call).
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from redis.asyncio import Redis

log = logging.getLogger("redis_budget")


SOCKET_TIMEOUT = 30.0  # seconds; must exceed every BLOCK we use (5s in the live profile)


class CountingRedis(Redis):
    command_count = 0

    @classmethod
    def from_url(cls, url: str, **kwargs):
        """Like Redis.from_url, but with a blocking pool: when every connection is busy a caller
        waits its turn (up to `timeout`) instead of failing with "Too many connections"."""
        from redis.asyncio import BlockingConnectionPool

        kwargs.setdefault("max_connections", 50)
        kwargs.setdefault("timeout", 10)
        # redis-py 8 times a read out after 5s by default, which is exactly the live profile's
        # XREADGROUP BLOCK of 5000ms: every idle worker poll failed ("Timeout reading").
        # The socket timeout must be well above the longest blocking call.
        kwargs.setdefault("socket_timeout", SOCKET_TIMEOUT)
        kwargs.setdefault("socket_connect_timeout", 10)
        # A hosted Redis (Upstash) closes idle connections: ping before reuse, keep TCP alive.
        kwargs.setdefault("health_check_interval", 30)
        kwargs.setdefault("socket_keepalive", True)
        return cls(connection_pool=BlockingConnectionPool.from_url(url, **kwargs))

    async def execute_command(self, *args, **options):
        self.command_count += 1
        return await super().execute_command(*args, **options)


def _month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


class CommandBudget:
    def __init__(
        self,
        redis: CountingRedis,
        limit: int,
        guard_ratio: float,
        month: Callable[[], str] = _month,
    ):
        self.redis, self.limit, self.guard_ratio, self.month = redis, limit, guard_ratio, month
        self.used = 0
        self._flushed = 0

    @property
    def live_available(self) -> bool:
        return self.used < self.limit * self.guard_ratio

    def key(self) -> str:
        return f"budget:redis:{self.month()}"

    async def flush(self) -> int:
        """Add the commands counted since the last flush to the monthly total; returns the total."""
        count = self.redis.command_count
        delta, self._flushed = count - self._flushed, count
        key = self.key()
        if delta:
            total = await self.redis.incrby(key, delta)
            if total == delta:
                await self.redis.expire(key, 40 * 24 * 3600)
        else:
            total = int(await self.redis.get(key) or 0)
        self.used = total
        return total

    async def run(self, stop: asyncio.Event, interval: float = 60.0, min_delta: int = 20) -> None:
        """Flush every `interval`, but only if enough new commands piled up. Flushing is itself a
        command, so flushing an idle (sleeping) system would keep it from ever being quiet."""
        while not stop.is_set():
            try:
                if self.redis.command_count - self._flushed >= min_delta:
                    await self.flush()
            except Exception:
                log.exception("command budget flush failed; retrying")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
