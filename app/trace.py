"""Recorded traces: the schema, and the checks that say a trace really shows what it claims.

A trace is a JSON file in web/public/traces/. Its `events` are exactly what the live /ws sends
(`{id, stream_id, type, payload, ts}`, see app/event_publisher.py), recorded from the REAL engine
by scripts/record_trace.py. The site replays them through the same event store as live events.

The same `check_trace` runs in the recorder (so a bad recording is rejected and re-run) and in
tests/test_traces.py (so a committed file can never drift from what its preset promises).
"""

import re
from collections import Counter
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

EVENT_TYPE = re.compile(r"^[a-z]+\.[a-z_]+$")


class TraceEvent(BaseModel):
    id: int
    stream_id: str
    type: str = Field(pattern=EVENT_TYPE.pattern)
    payload: dict
    ts: datetime


class Trace(BaseModel):
    preset: str  # slug, also the file name
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    recorded_at: datetime
    snapshot: dict  # the world before the first event (workers idle), as /ws sends on connect
    events: list[TraceEvent] = Field(min_length=1)

    @model_validator(mode="after")
    def _events_ok(self) -> "Trace":
        ids = [e.id for e in self.events]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate event ids")
        order = [(e.ts, e.id) for e in self.events]
        if order != sorted(order):
            raise ValueError("events must be sorted by (ts, id)")
        if self.snapshot.get("type") != "snapshot":
            raise ValueError("snapshot must be a /ws snapshot message")
        return self


def _of(trace: Trace, type_: str) -> list[TraceEvent]:
    return [e for e in trace.events if e.type == type_]


def check_trace(trace: Trace) -> list[str]:
    """Problems with a trace, as plain sentences. Empty means it shows what its preset promises."""
    types = Counter(e.type for e in trace.events)
    problems: list[str] = []

    def need(cond: bool, message: str) -> None:
        if not cond:
            problems.append(message)

    succeeded = Counter(str(e.payload.get("job_id")) for e in _of(trace, "job.succeeded"))
    need(
        all(n == 1 for n in succeeded.values()),
        "a job has more than one job.succeeded event (completion must be idempotent)",
    )
    need(types["goal.created"] >= 1, "no goal.created")
    need(types["goal.succeeded"] >= 1, "no goal.succeeded: the goal did not finish")
    need(types["goal.failed"] == 0, "the goal failed")
    need(types["job.succeeded"] >= 4, "fewer than 4 jobs succeeded")

    if trace.preset == "happy-path":
        need(types["task.created"] >= 4, "the plan is not a multi-step DAG")
        need(types["job.retry_scheduled"] == 0 and types["job.dead"] == 0, "not a clean run")
    elif trace.preset == "retries":
        need(types["job.retry_scheduled"] >= 2, "fewer than 2 retries")
        need(types["job.dead"] == 0, "a job died; jobs must eventually succeed")
    elif trace.preset == "chaos-recovery":
        need(types["worker.killed"] >= 1, "no worker was killed")
        need(types["job.recovered"] >= 1, "no job was recovered from a dead worker")
        need(types["job.dead"] == 0, "a job died")
        killed = {e.payload.get("worker") for e in _of(trace, "worker.killed")}
        for e in _of(trace, "job.recovered"):
            jid = str(e.payload.get("job_id"))
            need(succeeded.get(jid) == 1, f"recovered job {jid[:8]} did not finish exactly once")
        need(bool(killed), "worker.killed has no worker id")
    elif trace.preset == "dlq-replay-approval":
        dead = {str(e.payload.get("job_id")): e for e in _of(trace, "job.dead")}
        need(len(dead) >= 1, "no job went to the DLQ")
        replays = _of(trace, "job.replayed")
        need(len(replays) >= 1, "no DLQ replay")
        need(types["task.awaiting_approval"] >= 1, "no approval gate")
        need(types["task.approved"] >= 1, "the approval was never clicked")
        for r in replays:
            jid = str(r.payload.get("job_id"))
            later = [
                e
                for e in _of(trace, "job.succeeded")
                if str(e.payload.get("job_id")) == jid and (e.ts, e.id) > (r.ts, r.id)
            ]
            need(jid in dead and len(later) == 1, f"replayed job {jid[:8]} did not then succeed")
    else:
        problems.append(f"unknown preset {trace.preset!r}")
    return problems
