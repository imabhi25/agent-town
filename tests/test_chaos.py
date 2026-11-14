"""End-to-end chaos: workers are killed and replaced while 200 jobs run. Nothing may be lost or
duplicated. Marked slow (several seconds) but it runs in CI like every other test."""

import pytest

from app.chaos import run_chaos


@pytest.mark.slow
async def test_killing_workers_while_jobs_run_loses_and_duplicates_nothing(pool, redis):
    r = await run_chaos(pool, redis, jobs=200, workers=4, seed=7)

    # The chaos must be real, or the assertions below prove nothing.
    assert r.kills >= 3
    assert r.recoveries >= 1

    assert r.succeeded == 200 and r.dead == 0
    assert r.one_output_per_job  # exactly one job_outputs row per job
    assert r.one_event_per_job  # exactly one job.succeeded event per job
    assert r.pending == 0  # XPENDING: every message acknowledged
