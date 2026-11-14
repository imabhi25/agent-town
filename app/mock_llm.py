"""Mock LLM provider: behaves like a flaky real one so chaos tests are reproducible."""

import asyncio
import random


class ProviderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class MockLLM:
    def __init__(
        self,
        failure_rate: float = 0.0,
        min_latency: float = 0.3,
        max_latency: float = 3.0,
        rng: random.Random | None = None,
    ):
        self.failure_rate = failure_rate
        self.min_latency = min_latency
        self.max_latency = max_latency
        self.rng = rng or random.Random()

    async def complete(self, prompt: str) -> str:
        await asyncio.sleep(self.rng.uniform(self.min_latency, self.max_latency))
        if self.rng.random() < self.failure_rate:
            status = self.rng.choice([429, 500, 503])
            raise ProviderError(status, f"mock provider error {status}")
        return f"[mock] result for: {prompt[:60]}"
