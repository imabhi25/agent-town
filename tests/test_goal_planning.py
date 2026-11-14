"""POST /goals -> planner job -> stored tasks, against real Postgres and Redis."""

from helpers import CountingMock, _acked, _wait_for
from test_planner import VALID, FakeClient

from app.budget import DailyCap
from app.dispatcher import STREAM, publish_pending
from app.planner import GeminiPlanner
from app.worker import WorkerPool


async def _plan_goal(client, pool, redis, planner=None) -> str:
    r = await client.post("/goals", json={"prompt": "launch a newsletter"})
    assert r.status_code == 201 and r.json()["status"] == "planning"
    goal_id = r.json()["id"]
    assert await publish_pending(pool, redis) == 1
    workers = WorkerPool(pool, redis, CountingMock(), size=2, block_ms=100, planner=planner)
    await workers.start()
    try:
        await _wait_for(
            lambda: pool.fetchval("SELECT status <> 'planning' FROM goals WHERE id = $1", goal_id)
        )
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()
    return goal_id


async def test_post_goal_queues_a_planner_job(client, pool):
    r = await client.post("/goals", json={"prompt": "launch a newsletter"})
    goal_id = r.json()["id"]
    job = await pool.fetchrow("SELECT type, payload, status FROM jobs")
    assert job["type"] == "planner" and job["status"] == "queued"
    assert goal_id in job["payload"]
    assert await pool.fetchval("SELECT count(*) FROM outbox") == 1  # same transaction as the goal
    assert await pool.fetchval("SELECT count(*) FROM events WHERE type = 'goal.created'") == 1


async def test_no_key_plan_is_stored_with_mock_source(client, pool, redis):
    goal_id = await _plan_goal(client, pool, redis, planner=None)
    goal = (await client.get(f"/goals/{goal_id}")).json()
    assert goal["status"] == "planned" and goal["plan_source"] == "mock"
    assert [t["key"] for t in goal["tasks"]] == ["t1", "t2", "t3", "t4"]
    by_key = {t["key"]: t for t in goal["tasks"]}
    # Planner ids were translated to real task UUIDs.
    assert by_key["t4"]["depends_on"] == [by_key["t2"]["id"], by_key["t3"]["id"]]
    assert by_key["t1"]["depends_on"] == []
    assert by_key["t4"]["needs_approval"] and by_key["t1"]["status"] == "pending"

    types = [r["type"] for r in await pool.fetch("SELECT type FROM events ORDER BY id")]
    assert types.count("goal.planned") == 1 and types.count("task.created") == 4
    assert types.index("goal.created") < types.index("goal.planned")


async def test_valid_gemini_plan_is_stored(client, pool, redis):
    gemini = GeminiPlanner(FakeClient(VALID), "m", DailyCap(redis, 50, lambda: "2026-02-01"))
    goal_id = await _plan_goal(client, pool, redis, planner=gemini)
    goal = (await client.get(f"/goals/{goal_id}")).json()
    assert goal["plan_source"] == "gemini" and len(goal["tasks"]) == 3
    await redis.delete("gemini:calls:2026-02-01")


async def test_invalid_gemini_output_stores_the_fallback_plan(client, pool, redis):
    client_double = FakeClient("garbage", "still garbage")
    gemini = GeminiPlanner(client_double, "m", DailyCap(redis, 50, lambda: "2026-02-02"))
    goal_id = await _plan_goal(client, pool, redis, planner=gemini)
    goal = (await client.get(f"/goals/{goal_id}")).json()
    assert goal["plan_source"] == "fallback" and len(goal["tasks"]) == 3
    assert len(client_double.calls) == 2
    await redis.delete("gemini:calls:2026-02-02")


async def test_redelivered_planner_job_stores_tasks_once(client, pool, redis):
    goal_id = await _plan_goal(client, pool, redis)
    # Deliver the same planner job again, as a dispatcher republish or crash redelivery would.
    await redis.xadd(STREAM, (await redis.xrange(STREAM))[0][1])
    workers = WorkerPool(pool, redis, CountingMock(), size=1, block_ms=100)
    await workers.start()
    try:
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()
    assert await pool.fetchval("SELECT count(*) FROM tasks WHERE goal_id = $1", goal_id) == 4
    assert await pool.fetchval("SELECT count(*) FROM events WHERE type = 'goal.planned'") == 1
    assert await pool.fetchval("SELECT count(*) FROM job_outputs") == 1
