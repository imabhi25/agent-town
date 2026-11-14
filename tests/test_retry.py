import random

import pytest

from app.providers.mock_llm import ProviderError
from app.retry import backoff_delay, is_retryable


def test_delay_is_within_full_jitter_window():
    rng = random.Random(42)
    for attempt in range(1, 8):
        ceiling = min(30.0, 1.0 * 2 ** (attempt - 1))
        for _ in range(200):
            assert 0 <= backoff_delay(attempt, rng=rng) <= ceiling


def test_ceiling_doubles_then_is_capped():
    class MaxRng:  # always picks the top of the window, which exposes the ceiling
        def uniform(self, a, b):
            return b

    delays = [backoff_delay(n, base=1.0, cap=30.0, rng=MaxRng()) for n in range(1, 8)]
    assert delays == [1, 2, 4, 8, 16, 30, 30]


def test_same_seed_gives_same_delays_and_jitter_actually_varies():
    a = [backoff_delay(3, rng=random.Random(7)) for _ in range(3)]
    assert a[0] == a[1] == a[2]  # a fresh Random(7) each time: deterministic
    rng = random.Random(7)
    varied = {backoff_delay(3, rng=rng) for _ in range(20)}
    assert len(varied) > 1


def test_custom_base_and_cap():
    class MaxRng:
        def uniform(self, a, b):
            return b

    assert backoff_delay(1, base=0.01, cap=0.05, rng=MaxRng()) == 0.01
    assert backoff_delay(10, base=0.01, cap=0.05, rng=MaxRng()) == 0.05


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_provider_statuses(status):
    assert is_retryable(ProviderError(status, "x"))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_retryable_provider_statuses(status):
    assert not is_retryable(ProviderError(status, "x"))


def test_timeout_is_retryable_and_unknown_errors_are_not():
    assert is_retryable(TimeoutError())
    assert not is_retryable(ValueError("bug"))
