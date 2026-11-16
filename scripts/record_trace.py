"""Record the four preset traces from the REAL engine (mock LLM, no API spend).

    python scripts/record_trace.py --reset            # all four -> web/public/traces/
    python scripts/record_trace.py --reset retries    # just one

It needs Postgres and Redis (docker compose up -d, or DATABASE_URL / REDIS_URL) and WIPES the
tables, so it refuses a non-empty database unless --reset is given.

What runs is the production code: the FastAPI app (called in-process through httpx, so the real
endpoints POST /goals, /chaos/*, /tasks/*/approve, /dlq/*/replay), the dispatcher, the event
publisher and a real worker pool. Only the clocks are short and the provider is the mock.
Events are read back from the Redis "events" stream, so the files hold exactly the
{id, stream_id, type, payload, ts} messages /ws sends. A take is kept only if app/trace.py's
check_trace agrees it shows what the preset promises, otherwise it is recorded again.
"""

import argparse
import asyncio
import contextlib
import json
import random
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
from redis.asyncio import from_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import create_pool
from app.dispatcher import STREAM, run_dispatcher
from app.event_publisher import EVENTS_STREAM, parse_event, run_event_publisher
from app.hub import build_snapshot
from app.loadgen import submit_mock_jobs
from app.providers.mock_llm import MockLLM
from app.recovery import RecoveryConfig
from app.retry import DELAYED_KEY, DLQ_STREAM, RetryPolicy
from app.trace import Trace, check_trace
from app.worker import WorkerPool

# The recorder submits free-text goals, so it runs the planner that accepts them (the mock planner:
# no key, no API spend). The public default is PLANNER=fixed.
settings.planner = "gemini"

OUT = Path(__file__).resolve().parent.parent / "web" / "public" / "traces"
TABLES = "TRUNCATE goals, tasks, jobs, job_outputs, outbox, events RESTART IDENTITY CASCADE"
# Slow enough to watch (workers are visibly busy for about a second), fast enough that a
# whole trace is 10-20 seconds. heartbeat_ttl < lease, as in production.
RECOVERY = RecoveryConfig(lease=2.0, interval=0.3, heartbeat_interval=0.2, heartbeat_ttl=1.0)


class Rig:
    """The running system plus a few helpers a scenario needs."""

    def __init__(self, pool, redis, client, workers: WorkerPool, provider: MockLLM):
        self.pool, self.redis, self.client = pool, redis, client
        self.workers, self.provider = workers, provider

    async def until(self, cond: Callable[[], Awaitable[bool]], what: str, timeout: float = 60):
        deadline = asyncio.get_event_loop().time() + timeout
        while not await cond():
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(f"timed out waiting for {what}")
            await asyncio.sleep(0.1)

    async def post(self, path: str, body: dict | None = None) -> dict:
        r = await self.client.post(path, json=body or {})
        r.raise_for_status()
        return r.json()

    async def goal(self, prompt: str) -> str:
        return (await self.post("/goals", {"prompt": prompt}))["id"]

    async def goal_status(self, goal_id: str) -> str:
        return await self.pool.fetchval("SELECT status FROM goals WHERE id = $1::uuid", goal_id)

    async def awaiting_approval(self, goal_id: str):
        return await self.pool.fetchval(
            "SELECT id FROM tasks WHERE goal_id = $1::uuid AND status = 'awaiting_approval'",
            goal_id,
        )

    async def wait_for_approval_gate(self, goal_id: str):
        async def waiting() -> bool:
            return await self.awaiting_approval(goal_id) is not None

        await self.until(waiting, "the approval gate")
        return await self.awaiting_approval(goal_id)

    async def approve(self, goal_id: str, think: float = 1.5) -> None:
        """Wait for the gate, pause like a person reading the card, then click Approve."""
        task_id = await self.wait_for_approval_gate(goal_id)
        await asyncio.sleep(think)
        await self.post(f"/tasks/{task_id}/approve")

    async def finish(self, goal_id: str) -> None:
        async def done() -> bool:
            return await self.goal_status(goal_id) in ("succeeded", "failed")

        await self.until(done, "the goal to finish")


# ---- the four scenarios ---------------------------------------------------------------------


async def happy_path(rig: Rig) -> None:
    goal = await rig.goal("Plan a trip to Montreal")
    await rig.approve(goal)
    await rig.finish(goal)


async def retries(rig: Rig) -> None:
    # Half of all provider calls fail with 429/500: jobs retry with backoff and get through.
    await rig.post("/chaos/mock", {"failure_rate": 0.5})
    goal = await rig.goal("Compare 3 laptops")
    await rig.approve(goal)
    await rig.finish(goal)


async def chaos_recovery(rig: Rig) -> None:
    await rig.post("/chaos/mock", {"min_latency": 1.0, "max_latency": 1.6})
    goal = await rig.goal("Write a launch checklist")
    await rig.post("/chaos/flood", {"count": 4})

    async def a_worker_is_busy() -> bool:
        rows = await rig.pool.fetch("SELECT locked_by FROM jobs WHERE status = 'running'")
        return any(r["locked_by"] in rig.workers.tasks for r in rows)

    await rig.until(a_worker_is_busy, "a worker to start a job")
    busy = await rig.pool.fetchval("SELECT locked_by FROM jobs WHERE status = 'running' LIMIT 1")
    await asyncio.sleep(0.3)  # the job is mid-flight now
    await rig.post("/chaos/kill", {"worker_id": busy})  # a crash: no XACK, heartbeat expires
    await rig.post("/chaos/start")  # a replacement joins; XAUTOCLAIM hands it the dead one's job
    await rig.approve(goal)
    await rig.finish(goal)

    async def flood_done() -> bool:
        n = await rig.pool.fetchval("SELECT count(*) FROM jobs WHERE status NOT IN ('succeeded')")
        return n == 0

    await rig.until(flood_done, "every job to succeed")


async def dlq_replay_approval(rig: Rig) -> None:
    goal = await rig.goal("Draft and publish a newsletter")
    await rig.wait_for_approval_gate(goal)  # t1..t3 done; the gate now waits for a click
    # The provider breaks. One job burns its 3 attempts with backoff and lands in the DLQ.
    await rig.post("/chaos/mock", {"failure_rate": 1.0})
    await submit_mock_jobs(rig.pool, 1, "doomed", max_attempts=3)

    async def dead() -> bool:
        return await rig.pool.fetchval("SELECT count(*) FROM jobs WHERE status = 'dead'") == 1

    await rig.until(dead, "the job to reach the DLQ")
    await asyncio.sleep(1.5)  # the gate has been waiting the whole time
    await rig.post("/chaos/mock", {"failure_rate": 0.0})  # provider recovered
    await rig.approve(goal, think=1.0)
    dead_id = await rig.pool.fetchval("SELECT id FROM jobs WHERE status = 'dead'")
    await asyncio.sleep(1.0)
    await rig.post(f"/dlq/{dead_id}/replay")  # fresh retry budget; it now succeeds
    await rig.finish(goal)

    async def replayed_done() -> bool:
        return (
            await rig.pool.fetchval("SELECT status FROM jobs WHERE id = $1", dead_id) == "succeeded"
        )

    await rig.until(replayed_done, "the replayed job to succeed")


PRESETS = {
    "happy-path": (
        "Happy path",
        (
            "A goal is planned into a 4-step task graph (two steps run in parallel) and every job "
            "succeeds. An approval gate pauses the last step until it is approved."
        ),
        happy_path,
    ),
    "retries": (
        "Retries with backoff",
        (
            "The provider fails half of its calls. Failed jobs retry with exponential backoff and "
            "jitter and eventually succeed; none are lost."
        ),
        retries,
    ),
    "chaos-recovery": (
        "Chaos recovery",
        (
            "A worker is killed in the middle of a job. Its heartbeat expires, another worker "
            "reclaims the job (XAUTOCLAIM) and it completes once in effect."
        ),
        chaos_recovery,
    ),
    "dlq-replay-approval": (
        "DLQ, approval and replay",
        (
            "A job exhausts its retries and lands in the dead-letter queue while an approval gate "
            "waits for a click. After the approval the dead job is replayed and succeeds."
        ),
        dlq_replay_approval,
    ),
}


# ---- recording ------------------------------------------------------------------------------


async def record_once(pool, redis, preset: str, seed: int) -> Trace:
    name, description, scenario = PRESETS[preset]
    from app.main import app

    await pool.execute(TABLES)
    await redis.delete(STREAM, DLQ_STREAM, DELAYED_KEY, EVENTS_STREAM)
    for key in [k async for k in redis.scan_iter(match="worker:hb:*")]:
        await redis.delete(key)

    rng = random.Random(seed)
    provider = MockLLM(failure_rate=0.0, min_latency=0.4, max_latency=0.9, rng=rng)
    workers = WorkerPool(
        pool,
        redis,
        provider,
        size=3,
        block_ms=50,
        policy=RetryPolicy(base=0.6, cap=2.5, rng=rng),
        mover_interval=0.1,
        recovery=RECOVERY,
    )
    # The module-level app is the real one; give it the pieces its lifespan would create.
    app.state.pool, app.state.redis, app.state.workers = pool, redis, workers
    for attr in ("guard", "gate"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)

    stop = asyncio.Event()
    background = [
        asyncio.create_task(run_dispatcher(pool, redis, stop, interval=0.05)),
        asyncio.create_task(run_event_publisher(pool, redis, stop, interval=0.05)),
    ]
    await workers.start()
    transport = httpx.ASGITransport(app=app)
    try:
        await asyncio.sleep(0.3)
        snapshot = await build_snapshot(pool, redis)
        async with httpx.AsyncClient(transport=transport, base_url="http://rec") as client:
            await scenario(Rig(pool, redis, client, workers, provider))

        async def published() -> bool:
            return (
                await pool.fetchval("SELECT count(*) FROM events WHERE published_at IS NULL") == 0
            )

        for _ in range(100):
            if await published():
                break
            await asyncio.sleep(0.1)
        entries = await redis.xrange(EVENTS_STREAM)
    finally:
        await workers.stop()
        stop.set()
        for t in background:
            with contextlib.suppress(Exception):
                await t
    events = sorted((parse_event(i, f) for i, f in entries), key=lambda e: (e["ts"], e["id"]))
    return Trace.model_validate(
        {
            "preset": preset,
            "name": name,
            "description": description,
            "recorded_at": datetime.now(UTC).isoformat(),
            "snapshot": snapshot,
            "events": events,
        }
    )


async def main(args: argparse.Namespace) -> bool:
    pool = await create_pool(min_size=1, max_size=10)
    redis = from_url(settings.redis_url, decode_responses=True)
    try:
        if await pool.fetchval("SELECT count(*) FROM jobs") and not args.reset:
            print("The jobs table is not empty. Re-run with --reset to wipe it first.")
            return False
        OUT.mkdir(parents=True, exist_ok=True)
        wanted = args.presets or list(PRESETS)
        ok = True
        for preset in wanted:
            for take in range(1, args.takes + 1):
                trace = await record_once(pool, redis, preset, seed=take)
                problems = check_trace(trace)
                print(
                    f"{preset} take {take}: {len(trace.events)} events, "
                    f"{'OK' if not problems else 'REJECTED: ' + '; '.join(problems)}"
                )
                if not problems:
                    (OUT / f"{preset}.json").write_text(
                        json.dumps(trace.model_dump(mode="json"), indent=1) + "\n"
                    )
                    break
            else:
                ok = False
        index = [
            {"preset": p, "name": PRESETS[p][0], "description": PRESETS[p][1]} for p in PRESETS
        ]
        (OUT / "index.json").write_text(json.dumps(index, indent=1) + "\n")
        return ok
    finally:
        await pool.execute(TABLES)
        await redis.aclose()
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("presets", nargs="*", help=f"any of: {', '.join(PRESETS)} (default: all)")
    parser.add_argument("--reset", action="store_true", help="wipe the tables first")
    parser.add_argument(
        "--takes", type=int, default=5, help="re-record up to N times if a take fails its checks"
    )
    sys.exit(0 if asyncio.run(main(parser.parse_args())) else 1)
