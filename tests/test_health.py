import random

from fastapi.testclient import TestClient

from app.main import app
from app.mock_llm import MockLLM, ProviderError


def test_health():
    # No `with` block, so the lifespan (database pool) is not started.
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}


async def test_mock_llm_succeeds_when_no_failures():
    llm = MockLLM(failure_rate=0.0, min_latency=0, max_latency=0)
    assert (await llm.complete("hello")).startswith("[mock]")


async def test_mock_llm_fails_when_always_failing():
    llm = MockLLM(failure_rate=1.0, min_latency=0, max_latency=0, rng=random.Random(1))
    try:
        await llm.complete("hello")
    except ProviderError as e:
        assert e.status in (429, 500, 503)
    else:
        raise AssertionError("expected ProviderError")
