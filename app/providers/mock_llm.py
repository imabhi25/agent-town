"""Mock LLM provider: behaves like a flaky real one so tests and chaos runs are reproducible.

Pure async: latency is asyncio.sleep, so a slow mock call never blocks the event loop (and can be
cancelled mid-sleep, which the kill-worker test relies on).

Behaviour per call is chosen, in order, by: the explicit `mode` argument (the worker passes the
job payload's `mock_mode`), then the random `failure_rate`, then success.
"""

import asyncio
import random
from enum import StrEnum

from app.config import settings


class FailureMode(StrEnum):
    SUCCESS = "success"
    RATE_LIMIT = "429"
    SERVER_ERROR = "500"
    BAD_REQUEST = "400"  # not retryable: goes straight to the DLQ
    TIMEOUT = "timeout"
    HANG = "hang"  # never answers: only a caller-side timeout can end the call


class ProviderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class MockLLM:
    def __init__(
        self,
        failure_rate: float = 0.0,
        min_latency: float | None = None,
        max_latency: float | None = None,
        rng: random.Random | None = None,
    ):
        self.failure_rate = failure_rate
        self.min_latency = settings.mock_min_latency if min_latency is None else min_latency
        self.max_latency = settings.mock_max_latency if max_latency is None else max_latency
        self.rng = rng or random.Random()

    async def complete(
        self,
        prompt: str,
        *,
        mode: FailureMode | str | None = None,
        latency: float | None = None,
    ) -> dict:
        """Return a deterministic JSON-serialisable result, or raise like a real provider would.

        `latency` (seconds) overrides the configured random latency for this one call.
        """
        if latency is None:
            latency = self.rng.uniform(self.min_latency, self.max_latency)
        await asyncio.sleep(latency)

        if mode is None and self.rng.random() < self.failure_rate:
            mode = self.rng.choice([FailureMode.RATE_LIMIT, FailureMode.SERVER_ERROR])
        mode = FailureMode(mode) if mode is not None else FailureMode.SUCCESS

        if mode is FailureMode.RATE_LIMIT:
            raise ProviderError(429, "mock provider error 429")
        if mode is FailureMode.SERVER_ERROR:
            raise ProviderError(500, "mock provider error 500")
        if mode is FailureMode.BAD_REQUEST:
            raise ProviderError(400, "mock provider error 400")
        if mode is FailureMode.HANG:
            await asyncio.sleep(3600)  # cancelled by the caller's asyncio.wait_for
        if mode is FailureMode.TIMEOUT:
            raise TimeoutError("mock provider timed out")
        return {"text": f"[mock] result for: {prompt[:60]}"}
