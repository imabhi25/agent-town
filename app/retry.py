"""Retry policy: pure functions plus the Redis key names used by retries and the DLQ.

Nothing here touches Redis or Postgres, so it is trivial to unit test.
"""

import random
from dataclasses import dataclass, field
from typing import Protocol

from app.config import settings
from app.providers.mock_llm import ProviderError

DELAYED_KEY = "jobs:delayed"  # sorted set: member = job id, score = unix time it is due
DLQ_STREAM = "dlq"  # dead jobs; Postgres jobs.status = 'dead' is the source of truth

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class _Uniform(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


def backoff_delay(
    attempt: int, base: float = 1.0, cap: float = 30.0, rng: _Uniform = random
) -> float:
    """Exponential backoff with FULL jitter: uniform(0, min(cap, base * 2**(attempt-1))).

    `attempt` is the 1-based number of the attempt that just failed. Full jitter (random across
    the whole window, not base +/- a bit) spreads retries out so many jobs failing together do not
    all come back at the same instant.
    """
    ceiling = min(cap, base * 2 ** (attempt - 1))
    return rng.uniform(0, ceiling)


def is_retryable(error: BaseException) -> bool:
    """Timeouts and rate-limit/server errors may succeed later; other errors (e.g. 400) will not."""
    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, ProviderError):
        return error.status in RETRYABLE_STATUSES
    return False


@dataclass
class RetryPolicy:
    base: float = field(default_factory=lambda: settings.retry_base_delay)
    cap: float = field(default_factory=lambda: settings.retry_max_delay)
    rng: _Uniform = random

    def delay(self, attempt: int) -> float:
        return backoff_delay(attempt, self.base, self.cap, self.rng)
