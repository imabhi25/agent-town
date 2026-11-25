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


async def test_progress_is_reported_in_steps_and_uses_the_build_time(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "task_build_seconds", 0.08)
    llm = MockLLM(min_latency=0, max_latency=0)
    seen: list[int] = []

    async def on_progress(p: int) -> None:
        seen.append(p)

    start = time.monotonic()
    await llm.complete("hello", on_progress=on_progress)
    assert seen == [25, 50, 75]  # 100 is the job.succeeded event, not a progress event
    assert time.monotonic() - start >= 0.08  # a goal task takes the build time, not 0s


async def test_a_failing_call_still_reports_progress_before_it_fails():
    llm = MockLLM(min_latency=0, max_latency=0)
    seen: list[int] = []

    async def on_progress(p: int) -> None:
        seen.append(p)

    with pytest.raises(ProviderError):
        await llm.complete("hello", mode="500", latency=0, on_progress=on_progress)
    assert seen == [25, 50, 75]
