"""Downstream protection for provider calls, plus the API-side guard for POST /goals.

Provider side (workers): before a call, `admit` checks the circuit breaker and a token bucket.
If either refuses, the job is DELAYED (put back on the delayed set), not failed and not charged an
attempt: the provider is struggling or we are calling it too fast, which is not the job's fault.
After a call, `record` feeds the breaker and emits circuit.opened / circuit.closed on transitions.
"""

import random
from dataclasses import dataclass

import asyncpg
from fastapi import Request
from redis.asyncio import Redis

from app.budget import DailyCap
from app.circuit import CircuitBreaker
from app.config import settings
from app.events import add_event
from app.ratelimit import TokenBucket
from app.redis_budget import CommandBudget
from app.sleep import SleepGate


@dataclass
class Admission:
    ok: bool
    wait: float = 0.0
    reason: str = ""  # "circuit" | "rate"


class Protection:
    def __init__(
        self,
        pool: asyncpg.Pool,
        redis: Redis,
        rate_per_sec: float | None = None,
        burst: float | None = None,
        threshold: int | None = None,
        open_seconds: float | None = None,
        probe_timeout: float | None = None,
    ):
        self.pool = pool
        self.rate = settings.provider_rate_per_sec if rate_per_sec is None else rate_per_sec
        self.burst = settings.provider_burst if burst is None else burst
        self.bucket = TokenBucket(redis)
        self.breaker = CircuitBreaker(
            redis,
            settings.circuit_failure_threshold if threshold is None else threshold,
            settings.circuit_open_seconds if open_seconds is None else open_seconds,
            settings.circuit_probe_timeout if probe_timeout is None else probe_timeout,
        )

    async def admit(self, provider: str) -> Admission:
        decision, wait = await self.breaker.allow(provider)
        if decision == "open":
            return Admission(False, wait, "circuit")
        ok, retry_after = await self.bucket.take(
            f"ratelimit:provider:{provider}", self.burst, self.rate
        )
        if not ok:
            if decision == "probe":
                await self.breaker.release_probe(provider)
            return Admission(False, retry_after, "rate")
        return Admission(True)

    async def record(self, provider: str, success: bool) -> None:
        transition = await self.breaker.record(provider, success)
        if transition:
            # The Lua script decided; the event is written right after. If we crash in between
            # the event is lost but the circuit state is right (events are for display).
            async with self.pool.acquire() as conn, conn.transaction():
                await add_event(conn, f"circuit.{transition}", {"provider": provider})


def deferral_delay(wait: float) -> float:
    """How long to park a refused job: the wait the limiter asked for, plus a little jitter so
    a burst of refused jobs does not all return at the same instant."""
    return max(wait, 0.1) + random.uniform(0, 0.25)


class GoalGuard:
    """Checks for POST /goals: command budget, per-IP token bucket, daily goal cap."""

    def __init__(
        self,
        redis: Redis,
        budget: CommandBudget | None,
        gate: SleepGate | None,
        capacity: float | None = None,
        refill_per_sec: float | None = None,
        daily_cap: int | None = None,
        trust_forwarded_for: bool | None = None,
    ):
        self.bucket = TokenBucket(redis)
        self.budget, self.gate = budget, gate
        self.capacity = settings.goal_rate_capacity if capacity is None else capacity
        self.refill = settings.goal_rate_per_sec if refill_per_sec is None else refill_per_sec
        limit = settings.live_goals_daily_cap if daily_cap is None else daily_cap
        self.daily = DailyCap(redis, limit, prefix="goals:live:") if limit > 0 else None
        self.limit = limit
        self.trust = (
            settings.trust_forwarded_for if trust_forwarded_for is None else trust_forwarded_for
        )

    @property
    def live_available(self) -> bool:
        return self.budget is None or self.budget.live_available

    def client_ip(self, request: Request) -> str:
        if self.trust:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def touch(self) -> None:
        if self.gate is not None:
            self.gate.touch()

    async def check_chaos(self, request: Request) -> tuple[int, str, dict] | None:
        """Per-IP limit for the chaos endpoints (they are public in the demo)."""
        ok, retry_after = await self.bucket.take(
            f"ratelimit:chaos:{self.client_ip(request)}",
            settings.chaos_rate_capacity,
            settings.chaos_rate_per_sec,
        )
        if ok:
            self.touch()
            return None
        wait = max(1, int(retry_after + 0.999))
        return 429, f"Too many chaos requests. Try again in {wait}s.", {"Retry-After": str(wait)}

    async def check(self, request: Request) -> tuple[int, str, dict] | None:
        """None if the goal may be created, else (status, message, headers)."""
        if not self.live_available:
            return (
                503,
                "Live mode is unavailable: the monthly Redis command budget is nearly used up. Use the recorded demo.",
                {},
            )
        ok, retry_after = await self.bucket.take(
            f"ratelimit:goals:{self.client_ip(request)}", self.capacity, self.refill
        )
        if not ok:
            wait = max(1, int(retry_after + 0.999))
            return (
                429,
                f"Too many goals from your address. Try again in {wait}s.",
                {"Retry-After": str(wait)},
            )
        if self.daily is not None and not await self.daily.acquire():
            return (
                429,
                (
                    f"The live demo's daily limit of {self.limit} goals has been reached. "
                    "Try again tomorrow, or use the recorded demo."
                ),
                {"Retry-After": "3600"},
            )
        self.touch()
        return None
