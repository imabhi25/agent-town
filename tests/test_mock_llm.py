import asyncio
import random
import time

import pytest

from app.providers.mock_llm import MockLLM, ProviderError


async def test_success_is_deterministic_json():
    llm = MockLLM(min_latency=0, max_latency=0)
    first = await llm.complete("hello")
    assert first == await llm.complete("hello")
    assert first["text"].startswith("[mock]")


@pytest.mark.parametrize("mode,status", [("429", 429), ("500", 500)])
async def test_forced_http_errors(mode, status):
    llm = MockLLM(min_latency=0, max_latency=0)
    with pytest.raises(ProviderError) as exc:
        await llm.complete("hello", mode=mode)
    assert exc.value.status == status


async def test_forced_timeout():
    llm = MockLLM(min_latency=0, max_latency=0)
    with pytest.raises(asyncio.TimeoutError):
        await llm.complete("hello", mode="timeout")


async def test_random_failure_rate_still_works():
    llm = MockLLM(failure_rate=1.0, min_latency=0, max_latency=0, rng=random.Random(1))
    with pytest.raises(ProviderError) as exc:
        await llm.complete("hello")
    assert exc.value.status in (429, 500)


async def test_latency_override_does_not_block_the_event_loop():
    llm = MockLLM()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    start = time.monotonic()
    await llm.complete("hello", latency=0.2)
    t.cancel()
    assert time.monotonic() - start >= 0.2
    assert ticks >= 5  # the loop kept running while the mock "waited"
