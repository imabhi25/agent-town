"""Sleep gate for the free-tier live demo: after `idle_seconds` with no new goals, the background
loops (workers, heartbeats, dispatcher, publisher) stop polling, so an idle deployment makes no
Redis commands and lets the database suspend. The next goal wakes everything.

Disabled (always awake) unless `enabled`, so tests and local runs behave as before.
"""

import asyncio
import contextlib
import time


class SleepGate:
    def __init__(self, enabled: bool = False, idle_seconds: float = 300.0):
        self.enabled = enabled
        self.idle_seconds = idle_seconds
        self.last_activity = time.monotonic()
        self._awake = asyncio.Event()
        self._awake.set()
        self._activity = asyncio.Event()
        self.delayed = asyncio.Event()  # set when a retry is scheduled (wakes the retry mover)

    @property
    def sleeping(self) -> bool:
        return not self._awake.is_set()

    def touch(self) -> None:
        """Something happened (a goal or chaos job was submitted): wake up and restart the clock."""
        self.last_activity = time.monotonic()
        self._awake.set()
        self._activity.set()

    def maybe_sleep(self) -> bool:
        """Fall asleep if idle for too long. Returns True if the gate is asleep afterwards."""
        if self.enabled and time.monotonic() - self.last_activity >= self.idle_seconds:
            self._awake.clear()
        return self.sleeping

    async def wait_awake(self, stop: asyncio.Event) -> None:
        """Return when awake or when `stop` is set, whichever is first."""
        if self._awake.is_set():
            return
        waiters = [asyncio.create_task(self._awake.wait()), asyncio.create_task(stop.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def idle_sleep(self, seconds: float) -> None:
        """Back-off sleep that a new goal cuts short."""
        self._activity.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._activity.wait(), timeout=seconds)


async def run_sleeper(gate: SleepGate, stop: asyncio.Event, check_every: float = 5.0) -> None:
    while not stop.is_set():
        gate.maybe_sleep()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=check_every)
