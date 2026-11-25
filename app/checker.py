"""Delivery-guarantee checker: reads Postgres after a chaos run and lists every violation.

It knows nothing about how the run went (which workers died, what Redis did). It only looks at
the final state of the database for the jobs that were submitted, so the same check works for an
in-process test, a multi-process campaign, or a production snapshot.

The invariants (README "Delivery guarantee"):
  - no lost job: every submitted job ends `succeeded` or `dead` (dead = in the DLQ, accounted for)
  - one stored result: a succeeded job has exactly one `job_outputs` row; any other job has none
  - one success effect: a succeeded job has exactly one `job.succeeded` event; any other has none

What is NOT a violation: a job running more than once. Delivery is at-least-once, so repeats are
expected; they are counted (`redelivered_jobs`) to show the guarantee was actually exercised.
"""

from dataclasses import dataclass, field

import asyncpg

TERMINAL = ("succeeded", "dead")
KINDS = (
    "lost",
    "missing_output",
    "duplicate_output",
    "orphan_output",
    "missing_effect",
    "duplicate_effect",
)


@dataclass
class Violation:
    job_id: str
    kind: str  # see KINDS
    detail: str


@dataclass
class CheckReport:
    jobs: int
    succeeded: int = 0
    dead: int = 0
    starts: int = 0  # job.started events: how many times work began, over all jobs
    redelivered_jobs: int = 0  # jobs whose work started more than once
    recoveries: int = 0  # job.recovered events: a dead (or silent) worker's job taken over
    retries: int = 0  # job.retry_scheduled events: provider errors
    violations: list[Violation] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(v.kind == kind for v in self.violations)

    @property
    def ok(self) -> bool:
        return not self.violations


async def check(pool: asyncpg.Pool, job_ids: list) -> CheckReport:
    ids = [str(j) for j in job_ids]
    report = CheckReport(jobs=len(ids))

    status = {
        str(r["id"]): r["status"]
        for r in await pool.fetch("SELECT id, status FROM jobs WHERE id = ANY($1::uuid[])", ids)
    }
    outputs = {
        str(r["job_id"]): r["n"]
        for r in await pool.fetch(
            "SELECT job_id, count(*) AS n FROM job_outputs WHERE job_id = ANY($1::uuid[]) "
            "GROUP BY job_id",
            ids,
        )
    }
    events: dict[tuple[str, str], int] = {}
    for r in await pool.fetch(
        """
        SELECT payload->>'job_id' AS job_id, type, count(*) AS n FROM events
        WHERE payload->>'job_id' = ANY($1::text[])
          AND type IN ('job.started', 'job.succeeded', 'job.recovered', 'job.retry_scheduled')
        GROUP BY 1, 2
        """,
        ids,
    ):
        events[(r["job_id"], r["type"])] = r["n"]

    for job_id in ids:
        st = status.get(job_id)
        n_out = outputs.get(job_id, 0)
        n_ok = events.get((job_id, "job.succeeded"), 0)
        starts = events.get((job_id, "job.started"), 0)
        report.starts += starts
        report.redelivered_jobs += starts > 1
        report.recoveries += events.get((job_id, "job.recovered"), 0)
        report.retries += events.get((job_id, "job.retry_scheduled"), 0)

        def flag(kind: str, detail: str, job_id: str = job_id) -> None:
            report.violations.append(Violation(job_id, kind, detail))

        if st is None:
            flag("lost", "job row missing")
            continue
        if st not in TERMINAL:
            flag("lost", f"still '{st}' after the run drained")
        if st == "succeeded":
            report.succeeded += 1
            if n_out == 0:
                flag("missing_output", "succeeded with no job_outputs row")
            elif n_out > 1:
                flag("duplicate_output", f"{n_out} job_outputs rows")
            if n_ok == 0:
                flag("missing_effect", "succeeded with no job.succeeded event")
            elif n_ok > 1:
                flag("duplicate_effect", f"{n_ok} job.succeeded events (want 1)")
        else:
            report.dead += st == "dead"
            if n_out:
                flag("orphan_output", f"{n_out} job_outputs rows on a '{st}' job")
            if n_ok:
                flag("duplicate_effect", f"{n_ok} job.succeeded events on a '{st}' job")
    return report
