"""Crash recovery building blocks: worker heartbeats and stale-message claiming.

Two different clocks are in play, and heartbeat_ttl must stay SHORTER than lease:
- heartbeat: a worker refreshes a Redis key (with a TTL) every heartbeat_interval. The key
  existing means "this worker process is alive". It is a separate task per worker, so it keeps
  beating while the worker is busy awaiting a slow provider call.
- lease: a stream message that has been pending longer than `lease` seconds is a candidate for
  XAUTOCLAIM. Whether the job is really abandoned is decided by the heartbeat, not the lease.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from redis.asyncio import Redis

from app.config import settings

log = logging.getLogger("recovery")


def heartbeat_key(worker_id: str) -> str:
    return f"worker:hb:{worker_id}"


async def beat(redis: Redis, worker_id: str, ttl: float, token: str = "1") -> None:
    """Refresh the heartbeat. Its value is the worker's lock token (one per worker START), so a
    reader can tell "this incarnation is alive" from "some process with this name is alive"."""
    await redis.set(heartbeat_key(worker_id), token, px=int(ttl * 1000))


async def is_alive(redis: Redis, worker_id: str) -> bool:
    return bool(await redis.exists(heartbeat_key(worker_id)))


async def heartbeat_token(redis: Redis, worker_id: str) -> str | None:
    """The lock token in the worker's heartbeat, or None if the heartbeat has expired."""
    return await redis.get(heartbeat_key(worker_id))


async def heartbeat_loop(
    redis: Redis,
    worker_id: str,
    stop: asyncio.Event,
    interval: float,
    ttl: float,
    gate=None,
    token: str = "1",
) -> None:
    while not stop.is_set():
        if gate is not None:
            await gate.wait_awake(stop)  # a sleeping worker does not beat; it beats on waking
            if stop.is_set():
                break
        try:
            await beat(redis, worker_id, ttl, token)
        except Exception:
            log.exception("%s: heartbeat failed; retrying", worker_id)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


@dataclass
class RecoveryConfig:
    lease: float = field(default_factory=lambda: settings.lease_seconds)
    interval: float = field(default_factory=lambda: settings.recovery_interval)
    heartbeat_interval: float = field(default_factory=lambda: settings.heartbeat_interval)
    heartbeat_ttl: float = field(default_factory=lambda: settings.heartbeat_ttl)


async def claim_stale(
    redis: Redis, stream: str, group: str, consumer: str, lease: float, count: int = 10
) -> list[tuple[str, dict]]:
    """XAUTOCLAIM messages idle longer than `lease` for `consumer`. Returns (id, fields) pairs.

    Claiming resets the message's idle time, so it will not be claimed again until another full
    lease passes. Messages deleted from the stream are skipped by Redis.
    """
    result = await redis.xautoclaim(
        stream, group, consumer, min_idle_time=int(lease * 1000), start_id="0-0", count=count
    )
    return [(msg_id, fields) for msg_id, fields in result[1] if fields]
