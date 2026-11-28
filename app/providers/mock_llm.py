"""Mock LLM provider: behaves like a flaky real one so tests and chaos runs are reproducible.

Pure async: latency is asyncio.sleep, so a slow mock call never blocks the event loop (and can be
cancelled mid-sleep, which the kill-worker test relies on).

Behaviour per call is chosen, in order, by: the explicit `mode` argument (the worker passes the
job payload's `mock_mode`), then the random `failure_rate`, then success.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from enum import StrEnum

from app.config import settings


class FailureMode(StrEnum):
    SUCCESS = "success"
    RATE_LIMIT = "429"
    SERVER_ERROR = "500"
    BAD_REQUEST = "400"  # not retryable: goes straight to the DLQ
    TIMEOUT = "timeout"
    HANG = "hang"  # never answers: only a caller-side timeout can end the call


PROGRESS_STEPS = 4  # a call that reports progress reports 25, 50 and 75 (100 is job.succeeded)

ProgressFn = Callable[[int], Awaitable[None]]


class ProviderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# Errors a provider returns without doing the work (used to end a progress-reporting call early).
ANSWERS_AT_ONCE = (FailureMode.RATE_LIMIT, FailureMode.SERVER_ERROR, FailureMode.BAD_REQUEST)


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

    def _pick(self, mode: FailureMode | str | None) -> FailureMode:
        """The explicit mode, else a random failure at `failure_rate`, else success."""
        if mode is None and self.rng.random() < self.failure_rate:
            mode = self.rng.choice([FailureMode.RATE_LIMIT, FailureMode.SERVER_ERROR])
        return FailureMode(mode) if mode is not None else FailureMode.SUCCESS

    async def complete(
        self,
        prompt: str,
        *,
        mode: FailureMode | str | None = None,
        latency: float | None = None,
        on_progress: ProgressFn | None = None,
    ) -> dict:
        """Return a deterministic JSON-serialisable result, or raise like a real provider would.

        `latency` (seconds) overrides the configured random latency for this one call.
        `on_progress` (a goal's task job): the call takes `settings.task_build_seconds` unless
        `latency` says otherwise, split into PROGRESS_STEPS sleeps, and reports the percentage
        done after each sleep but the last (100% is the job.succeeded event).
        """
        if on_progress is None:
            if latency is None:
                latency = self.rng.uniform(self.min_latency, self.max_latency)
            await asyncio.sleep(latency)
            mode = self._pick(mode)
        else:
            mode = self._pick(mode)  # known up front: a failing build stops early
            # A goal task: the work takes the build time and is reported in steps. An error ends
            # the work early (a real provider answers 429/500 without doing the work), so a
            # failing attempt reports one step and then fails, and a retry does not cost a full
            # build. HANG and TIMEOUT still wait, as without progress.
            if latency is None:
                latency = settings.task_build_seconds
            steps = 1 if mode in ANSWERS_AT_ONCE else PROGRESS_STEPS
            for step in range(1, steps + 1):
                await asyncio.sleep(latency / PROGRESS_STEPS)
                if step < PROGRESS_STEPS:
                    await on_progress(100 * step // PROGRESS_STEPS)

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
