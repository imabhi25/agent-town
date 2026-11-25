"""The checker must catch every kind of violation, or a clean campaign proves nothing.

Each test writes a database state by hand (no workers), then checks the checker's verdict.
The unsafe-mode test shows the same thing end to end: the deliberately broken worker loses a job
when it is killed mid-call, and the checker reports it.
"""

import json

from helpers import _make_job, _pending, _wait_for

from app.checker import check
from app.dispatcher import publish_pending
from app.providers.mock_llm import MockLLM
from app.worker import WorkerPool


async def _job(pool, status: str, outputs: int = 0, ok_events: int = 0, starts: int = 1) -> str:
    async with pool.acquire() as conn, conn.transaction():
        job_id = await conn.fetchval(
            "INSERT INTO jobs (type, payload, status) VALUES ('llm.mock', '{}', $1) RETURNING id",
            status,
        )
        if outputs:
            await conn.execute(
                "INSERT INTO job_outputs (job_id, output) VALUES ($1, '{}')", job_id
            )
        for kind, n in (("job.started", starts), ("job.succeeded", ok_events)):
            for _ in range(n):
                await conn.execute(
                    "INSERT INTO events (type, payload) VALUES ($1, $2::jsonb)",
                    kind,
                    json.dumps({"job_id": str(job_id)}),
                )
    return str(job_id)


async def test_clean_state_has_no_violations(pool):
    ids = [
        await _job(pool, "succeeded", outputs=1, ok_events=1),
        await _job(pool, "succeeded", outputs=1, ok_events=1, starts=3),  # redelivered: fine
        await _job(pool, "dead"),  # in the DLQ: accounted for
    ]
    r = await check(pool, ids)
    assert r.ok, r.violations
    assert (r.succeeded, r.dead, r.redelivered_jobs, r.starts) == (2, 1, 1, 5)


async def test_each_violation_kind_is_caught(pool):
    cases = {
        "lost": await _job(pool, "running"),
        "missing_output": await _job(pool, "succeeded", ok_events=1),
        "orphan_output": await _job(pool, "dead", outputs=1),
        "missing_effect": await _job(pool, "succeeded", outputs=1),
        "duplicate_effect": await _job(pool, "succeeded", outputs=1, ok_events=2),
    }
    r = await check(pool, list(cases.values()))
    found = {(v.kind, v.job_id) for v in r.violations}
    assert found == {(kind, job_id) for kind, job_id in cases.items()}


async def test_missing_job_row_counts_as_lost(pool):
    r = await check(pool, ["00000000-0000-0000-0000-000000000001"])
    assert r.count("lost") == 1


async def test_unsafe_ack_before_commit_loses_a_killed_job(client, pool, redis):
    """The deliberate bug: XACK before the commit. Kill the worker mid-call and the message is
    already acknowledged, so no one ever reclaims the job. The safe worker leaves it pending."""
    job_id = await _make_job(client, "k1", {"prompt": "slow", "mock_latency": 30})
    assert await publish_pending(pool, redis) == 1

    workers = WorkerPool(pool, redis, MockLLM(), size=1, block_ms=100)
    workers.extras.unsafe_ack_before_commit = True
    await workers.start()
    try:
        owner = await _wait_for(
            lambda: pool.fetchval(
                "SELECT locked_by FROM jobs WHERE id = $1::uuid AND status = 'running'", job_id
            )
        )
        await _wait_for(lambda: _no_pending(redis))
        await workers.kill_worker(owner)
    finally:
        await workers.stop()

    assert await _pending(redis) == 0  # acknowledged: nothing left to reclaim
    r = await check(pool, [job_id])
    assert [v.kind for v in r.violations] == ["lost"]


async def _no_pending(redis) -> bool:
    return await _pending(redis) == 0
