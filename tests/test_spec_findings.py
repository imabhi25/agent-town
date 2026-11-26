"""Regression tests for the two bugs the TLA+ model found (spec/README.md).

1. The delayed-set mover did XADD then a plain ZREM. If a worker rescheduled the same job in
   between (failed again: ZADD with a later score), the ZREM deleted the NEW entry, the old
   message was acknowledged, and the job sat in 'retrying' with nothing left to run it.
2. "Busy" meant "a worker with that name has a heartbeat". A worker that crashed and restarted
   under the same name beats again without running the job, so other workers left the job
   alone; only the restarted worker itself could take it back.
"""

import time

from helpers import _make_job

from app.dispatcher import STREAM
from app.recovery import beat
from app.retry import DELAYED_KEY
from app.worker import _claim, move_due_jobs


async def _set_running(pool, job_id: str, worker: str, token: str | None) -> None:
    await pool.execute(
        "UPDATE jobs SET status = 'running', locked_by = $2, lock_token = $3, attempts = 1 "
        "WHERE id = $1::uuid",
        job_id,
        worker,
        token,
    )


async def test_mover_keeps_an_entry_rescheduled_during_the_move(client, pool, redis):
    job_id = await _make_job(client, "spec-mover-race")
    await pool.execute("UPDATE jobs SET status = 'retrying' WHERE id = $1::uuid", job_id)
    due = time.time() - 1
    await redis.zadd(DELAYED_KEY, {job_id: due})
    later = time.time() + 60

    real_xadd = redis.xadd

    async def xadd_then_worker_reschedules(*args, **kwargs):
        result = await real_xadd(*args, **kwargs)
        # Between the mover's XADD and its ZREM: a worker takes the new message, the provider
        # fails again, and the job is rescheduled with a later score.
        await redis.zadd(DELAYED_KEY, {job_id: later})
        return result

    redis.xadd = xadd_then_worker_reschedules
    try:
        assert await move_due_jobs(pool, redis) == 1
    finally:
        redis.xadd = real_xadd

    assert await redis.zscore(DELAYED_KEY, job_id) == later  # the new entry survived
    assert await redis.xlen(STREAM) == 1


async def test_mover_removes_the_entry_it_moved(client, pool, redis):
    job_id = await _make_job(client, "spec-mover-plain")
    await pool.execute("UPDATE jobs SET status = 'retrying' WHERE id = $1::uuid", job_id)
    await redis.zadd(DELAYED_KEY, {job_id: time.time() - 1.234567891})
    assert await move_due_jobs(pool, redis) == 1
    assert await redis.zcard(DELAYED_KEY) == 0


async def test_restarted_holder_does_not_keep_its_job_busy(client, pool, redis):
    job_id = await _make_job(client, "spec-restart")
    await _set_running(pool, job_id, "worker-1", "token-before-crash")
    await beat(redis, "worker-1", 30, "token-after-restart")  # same name, new incarnation

    outcome, _ = await _claim(pool, redis, job_id, "worker-2", "token-w2")

    assert outcome == "claimed"
    row = await pool.fetchrow("SELECT locked_by, lock_token FROM jobs WHERE id = $1::uuid", job_id)
    assert (row["locked_by"], row["lock_token"]) == ("worker-2", "token-w2")
    recovered = await pool.fetchval(
        "SELECT count(*) FROM events WHERE type = 'job.recovered' AND payload->>'job_id' = $1",
        job_id,
    )
    assert recovered == 1


async def test_live_holder_keeps_its_job(client, pool, redis):
    job_id = await _make_job(client, "spec-live")
    await _set_running(pool, job_id, "worker-1", "token-1")
    await beat(redis, "worker-1", 30, "token-1")  # the incarnation that took the lock

    outcome, _ = await _claim(pool, redis, job_id, "worker-2", "token-w2")

    assert outcome == "busy"
    assert await pool.fetchval("SELECT locked_by FROM jobs WHERE id = $1::uuid", job_id) == (
        "worker-1"
    )


async def test_dead_holder_is_taken_over(client, pool, redis):
    job_id = await _make_job(client, "spec-dead")
    await _set_running(pool, job_id, "worker-1", "token-1")  # no heartbeat at all

    outcome, _ = await _claim(pool, redis, job_id, "worker-2", "token-w2")
    assert outcome == "claimed"


async def test_lock_without_token_falls_back_to_the_name(client, pool, redis):
    """Rows locked before lock tokens existed keep the old rule: a live name means busy."""
    job_id = await _make_job(client, "spec-legacy")
    await _set_running(pool, job_id, "worker-1", None)
    await beat(redis, "worker-1", 30)

    outcome, _ = await _claim(pool, redis, job_id, "worker-2", "token-w2")
    assert outcome == "busy"
