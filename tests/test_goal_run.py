"""Running a goal end to end: dependencies, approvals, failures, idempotent completion.

Real Postgres and Redis, mock provider, a real dispatcher loop and a real worker pool.
"""

import asyncio
import json

import pytest
from helpers import CountingMock, _wait_for

from app import goals
from app.dispatcher import run_dispatcher
from app.worker import WorkerPool


@pytest.fixture
async def system(pool, redis):
    """Dispatcher + 3 workers running in the background."""
    stop = asyncio.Event()
    dispatcher = asyncio.create_task(run_dispatcher(pool, redis, stop, interval=0.05))
    provider = CountingMock()
    workers = WorkerPool(pool, redis, provider, size=3, block_ms=100, mover_interval=0.1)
    await workers.start()
    yield SimpleSystem(provider)
    stop.set()
    await dispatcher
    await workers.stop()


class SimpleSystem:
    def __init__(self, provider):
        self.provider = provider


async def _goal_status(pool, goal_id):
    return await pool.fetchval("SELECT status FROM goals WHERE id = $1", goal_id)


async def _until_goal(pool, goal_id, status, timeout=8.0):
    await _wait_for(lambda: _is(pool, goal_id, status), timeout)


async def _is(pool, goal_id, status):
    return await _goal_status(pool, goal_id) == status


async def _submit(client) -> str:
    return (await client.post("/goals", json={"prompt": "launch a newsletter"})).json()["id"]


async def _task(pool, goal_id, key):
    return await pool.fetchrow("SELECT * FROM tasks WHERE goal_id = $1 AND key = $2", goal_id, key)


async def _event_types(pool) -> list[str]:
    return [r["type"] for r in await pool.fetch("SELECT type FROM events ORDER BY id")]


async def _approve_when_waiting(client, pool, goal_id, key="t4"):
    async def waiting():
        task = await _task(pool, goal_id, key)  # None until the planner job has stored the plan
        return task is not None and task["status"] == "awaiting_approval"

    await _wait_for(waiting)
    task = await _task(pool, goal_id, key)
    return task["id"]


async def test_goal_runs_to_success_with_ordering_and_dependency_outputs(client, pool, system):
    goal_id = await _submit(client)
    task_id = await _approve_when_waiting(client, pool, goal_id)

    # The approval task has not run, so the goal is not finished yet.
    assert await _goal_status(pool, goal_id) == "running"
    assert (await client.post(f"/tasks/{task_id}/approve")).json()["status"] == "approved"
    await _until_goal(pool, goal_id, "succeeded")

    goal = (await client.get(f"/goals/{goal_id}")).json()
    assert [t["status"] for t in goal["tasks"]] == ["succeeded"] * 4
    assert [t["key"] for t in goal["result"]["tasks"]] == ["t1", "t2", "t3", "t4"]

    # Ordering: a task is queued only after every dependency has succeeded.
    rows = await pool.fetch(
        "SELECT id, type, payload->>'task_id' AS task_id FROM events ORDER BY id"
    )
    events = [(r["type"], r["task_id"]) for r in rows]
    key_of = {str(t["id"]): t["key"] for t in goal["tasks"]}
    order = [(t, key_of.get(i)) for t, i in events if t in ("task.queued", "task.succeeded")]
    pos = {e: n for n, e in enumerate(order)}
    assert pos[("task.succeeded", "t1")] < pos[("task.queued", "t2")]
    assert pos[("task.succeeded", "t1")] < pos[("task.queued", "t3")]
    assert pos[("task.succeeded", "t3")] < pos[("task.queued", "t4")]
    assert pos[("task.succeeded", "t2")] < pos[("task.queued", "t4")]

    # Dependency outputs were passed into the prompt of the dependent task.
    prompt = json.loads(
        await pool.fetchval("SELECT payload FROM jobs WHERE task_id = $1", task_id)
    )["prompt"]
    assert "Review and publish" in prompt
    assert "Outline" in prompt and "Draft" in prompt and "[mock] result for" in prompt
    types = await _event_types(pool)
    assert types.count("goal.succeeded") == 1 and types.count("task.awaiting_approval") == 1

    # Every task job reported 25/50/75 while it ran, before its job.succeeded (the town raises
    # each building from these). The planner job is not a building and reports nothing.
    rows = await pool.fetch(
        "SELECT type, payload FROM events WHERE type IN ('job.progress', 'job.succeeded') "
        "ORDER BY id"
    )
    task_jobs = {str(r["id"]) for r in await pool.fetch("SELECT id FROM jobs WHERE type = 'task'")}
    progress: dict[str, list[int]] = {}
    for r in rows:
        p = json.loads(r["payload"])
        if r["type"] == "job.progress":
            assert "job_id" in p and p["worker"]
            progress.setdefault(p["job_id"], []).append(p["percent"])
        elif p["job_id"] in task_jobs:
            assert progress.get(p["job_id"]) == [25, 50, 75]
    assert set(progress) == task_jobs


async def test_parallel_tasks_are_queued_together(client, pool, redis):
    """t2 and t3 both depend only on t1, so both are queued the moment t1 succeeds."""
    goal_id = await _submit(client)
    from app.dispatcher import publish_pending

    assert await publish_pending(pool, redis) == 1  # planner job
    workers = WorkerPool(pool, redis, CountingMock(), size=2, block_ms=100)
    await workers.start()
    try:

        async def t1_queued():
            t = await _task(pool, goal_id, "t1")
            return t and t["status"] == "queued"

        await _wait_for(t1_queued)
        assert (await _task(pool, goal_id, "t2"))["status"] == "pending"
        assert await publish_pending(pool, redis) == 1  # t1's job
        await _wait_for(
            lambda: pool.fetchval(
                "SELECT count(*) = 2 FROM tasks WHERE goal_id = $1 AND status = 'queued'", goal_id
            )
        )
    finally:
        await workers.stop()
    statuses = {r["key"]: r["status"] for r in await pool.fetch("SELECT key, status FROM tasks")}
    assert statuses == {"t1": "succeeded", "t2": "queued", "t3": "queued", "t4": "pending"}


async def test_task_requiring_approval_waits_until_approved(client, pool, system):
    goal_id = await _submit(client)
    task_id = await _approve_when_waiting(client, pool, goal_id)
    await asyncio.sleep(0.5)
    assert (await _task(pool, goal_id, "t4"))["status"] == "awaiting_approval"
    assert await pool.fetchval("SELECT count(*) FROM jobs WHERE task_id = $1", task_id) == 0
    assert (await client.post(f"/tasks/{task_id}/approve")).status_code == 200
    # Approving twice (or approving a task that is not waiting) is a conflict, not a second job.
    assert (await client.post(f"/tasks/{task_id}/approve")).status_code == 409
    await _until_goal(pool, goal_id, "succeeded")
    assert await pool.fetchval("SELECT count(*) FROM jobs WHERE task_id = $1", task_id) == 1
    assert (await client.post(f"/tasks/{task_id}/reject")).status_code == 409


async def test_rejection_fails_the_goal(client, pool, system):
    goal_id = await _submit(client)
    task_id = await _approve_when_waiting(client, pool, goal_id)
    assert (await client.post(f"/tasks/{task_id}/reject")).json()["status"] == "rejected"
    await _until_goal(pool, goal_id, "failed")
    goal = (await client.get(f"/goals/{goal_id}")).json()
    assert goal["error"] == "task rejected"
    assert (await _task(pool, goal_id, "t4"))["status"] == "rejected"
    types = await _event_types(pool)
    assert types.count("goal.failed") == 1 and "task.rejected" in types
    assert (await client.post(f"/tasks/{task_id}/approve")).status_code == 409


async def test_unknown_task_is_404(client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.post(f"/tasks/{missing}/approve")).status_code == 404
    assert (await client.post(f"/tasks/{missing}/reject")).status_code == 404


async def test_dead_task_job_fails_the_goal(client, pool, redis):
    goal_id = await _submit(client)
    from app.dispatcher import publish_pending

    assert await publish_pending(pool, redis) == 1
    workers = WorkerPool(pool, redis, CountingMock(), size=2, block_ms=100)
    await workers.start()
    try:

        async def t1_job():
            return await pool.fetchval(
                "SELECT j.id FROM jobs j JOIN tasks t ON t.id = j.task_id "
                "WHERE t.goal_id = $1 AND t.key = 't1'",
                goal_id,
            )

        job_id = await _wait_for(t1_job)
        # A 400 from the provider is not retryable: the job goes straight to the DLQ.
        await pool.execute(
            'UPDATE jobs SET payload = payload || \'{"mock_mode": "400"}\' WHERE id = $1', job_id
        )
        await publish_pending(pool, redis)
        await _until_goal(pool, goal_id, "failed")
    finally:
        await workers.stop()

    assert (await _task(pool, goal_id, "t1"))["status"] == "failed"
    for key in ("t2", "t3", "t4"):
        assert (await _task(pool, goal_id, key))["status"] == "cancelled"
    assert (await pool.fetchval("SELECT status FROM jobs WHERE id = $1", job_id)) == "dead"
    types = await _event_types(pool)
    assert types.count("goal.failed") == 1 and "task.failed" in types
    assert (await pool.fetchrow("SELECT error FROM goals"))["error"].startswith("task failed")


async def test_dead_planner_job_fails_the_goal(client, pool, redis):
    goal_id = await _submit(client)
    from app.dispatcher import publish_pending

    await pool.execute(
        "UPDATE jobs SET payload = payload || '{\"mock_mode\": \"400\"}' WHERE type = 'planner'"
    )
    await publish_pending(pool, redis)

    class DeadPlanner:
        async def plan(self, prompt):
            from app.providers.mock_llm import ProviderError

            raise ProviderError(400, "bad request")

    workers = WorkerPool(pool, redis, CountingMock(), size=1, block_ms=100, planner=DeadPlanner())
    await workers.start()
    try:
        await _until_goal(pool, goal_id, "failed")
    finally:
        await workers.stop()
    assert (await pool.fetchrow("SELECT error FROM goals"))["error"].startswith("planning failed")


async def test_duplicate_completion_enqueues_dependents_once(client, pool, redis):
    """Run t1's completion transaction twice, as a redelivered message would."""
    goal_id = await _submit(client)
    from app.dispatcher import publish_pending

    await publish_pending(pool, redis)
    workers = WorkerPool(pool, redis, CountingMock(), size=1, block_ms=100)
    await workers.start()
    try:
        await _wait_for(
            lambda: pool.fetchval("SELECT count(*) = 1 FROM tasks WHERE status='queued'")
        )
    finally:
        await workers.stop()

    t1 = await _task(pool, goal_id, "t1")
    for _ in range(3):
        async with pool.acquire() as conn, conn.transaction():
            await goals.complete_task(conn, t1["id"], {"text": "done"})
            await goals.advance_goal(conn, goal_id)

    queued = await pool.fetch(
        "SELECT key FROM tasks WHERE goal_id = $1 AND status = 'queued'", goal_id
    )
    assert sorted(r["key"] for r in queued) == ["t2", "t3"]
    assert await pool.fetchval("SELECT count(*) FROM jobs WHERE type = 'task'") == 3  # t1, t2, t3
    types = await _event_types(pool)
    assert types.count("task.succeeded") == 1
    assert types.count("task.queued") == 3


async def test_concurrent_sibling_completions_enqueue_the_join_task_once(client, pool, redis):
    """t2 and t3 finish at the same moment; t4 (awaiting both) must be started exactly once."""
    goal_id = await _submit(client)
    from app.dispatcher import publish_pending

    await publish_pending(pool, redis)
    workers = WorkerPool(pool, redis, CountingMock(), size=1, block_ms=100)
    await workers.start()
    try:
        await _wait_for(
            lambda: pool.fetchval("SELECT count(*) = 1 FROM tasks WHERE status='queued'")
        )
    finally:
        await workers.stop()
    t1 = await _task(pool, goal_id, "t1")
    async with pool.acquire() as conn, conn.transaction():
        await goals.complete_task(conn, t1["id"], {"text": "r"})

    t2, t3 = await _task(pool, goal_id, "t2"), await _task(pool, goal_id, "t3")

    async def finish(task):
        async with pool.acquire() as conn, conn.transaction():
            await goals.complete_task(conn, task["id"], {"text": "x"})

    # Deterministic overlap: A completes t2 and stays uncommitted (holding the goal lock) while
    # B completes t3. B must wait for A; without the lock B would not see t2 as done and nobody
    # would ever start t4.
    async with pool.acquire() as a:
        tx = a.transaction()
        await tx.start()
        await goals.complete_task(a, t2["id"], {"text": "x"})
        b = asyncio.create_task(finish(t3))
        await asyncio.sleep(0.3)
        assert not b.done()  # blocked on the goal row lock
        await tx.commit()
        await b
    types = await _event_types(pool)
    assert types.count("task.awaiting_approval") == 1
    assert (await _task(pool, goal_id, "t4"))["status"] == "awaiting_approval"
