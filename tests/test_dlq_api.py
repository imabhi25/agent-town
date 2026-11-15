"""POST /dlq/{id}/replay and the dead_jobs list in the snapshot."""

from uuid import uuid4

from helpers import _make_job

from app.hub import build_snapshot
from app.main import app


async def _kill_job(pool, job_id: str) -> None:
    await pool.execute(
        "UPDATE jobs SET status = 'dead', updated_at = now() WHERE id = $1::uuid", job_id
    )


async def test_replay_requeues_a_dead_job(client, pool, redis):
    app.state.redis = redis
    job_id = await _make_job(client, "dead-1")
    await _kill_job(pool, job_id)
    r = await client.post(f"/dlq/{job_id}/replay")
    assert r.status_code == 200 and r.json() == {"id": job_id, "status": "queued"}
    assert await pool.fetchval("SELECT status FROM jobs WHERE id = $1::uuid", job_id) == "queued"
    events = await pool.fetch("SELECT type FROM events WHERE type = 'job.replayed'")
    assert len(events) == 1


async def test_replay_refuses_jobs_that_are_not_dead(client, pool, redis):
    app.state.redis = redis
    job_id = await _make_job(client, "alive-1")
    assert (await client.post(f"/dlq/{job_id}/replay")).status_code == 409
    assert (await client.post(f"/dlq/{uuid4()}/replay")).status_code == 409
    assert (await client.post("/dlq/not-a-uuid/replay")).status_code == 422


async def test_snapshot_lists_dead_jobs_and_replay_removes_them(client, pool, redis):
    app.state.redis = redis
    dead, alive = await _make_job(client, "dead-2"), await _make_job(client, "alive-2")
    await _kill_job(pool, dead)
    snap = await build_snapshot(pool, redis, worker_ids=[])
    assert [d["job_id"] for d in snap["dead_jobs"]] == [dead]
    assert alive not in [d["job_id"] for d in snap["dead_jobs"]]
    await client.post(f"/dlq/{dead}/replay")
    assert (await build_snapshot(pool, redis, worker_ids=[]))["dead_jobs"] == []
