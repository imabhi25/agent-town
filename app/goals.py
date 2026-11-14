"""Goal and task persistence. Every state change writes its event in the same transaction."""

import json
from uuid import UUID, uuid4

import asyncpg

from app.events import add_event
from app.planner import PlanModel


def planner_idempotency_key(goal_id: UUID) -> str:
    return f"planner:{goal_id}"


async def create_goal(conn: asyncpg.Connection, prompt: str) -> UUID:
    """Goal + its planner job + outbox row + goal.created, all in the caller's transaction."""
    goal_id = await conn.fetchval("INSERT INTO goals (prompt) VALUES ($1) RETURNING id", prompt)
    job_id = await conn.fetchval(
        """
        INSERT INTO jobs (type, payload, idempotency_key, max_attempts)
        VALUES ('planner', $1::jsonb, $2, 3) RETURNING id
        """,
        json.dumps({"goal_id": str(goal_id), "prompt": prompt}),
        planner_idempotency_key(goal_id),
    )
    await conn.execute("INSERT INTO outbox (job_id) VALUES ($1)", job_id)
    await add_event(conn, "goal.created", {"goal_id": str(goal_id), "job_id": str(job_id)})
    return goal_id


async def store_plan(conn: asyncpg.Connection, goal_id: UUID, plan: PlanModel, source: str) -> bool:
    """Store the tasks of a validated plan. Runs inside the planner job's completion transaction.

    Idempotent: only a goal still in 'planning' is planned, so a redelivered planner job (which
    never reaches here anyway, see _complete) could not store the tasks twice.
    Returns False if the goal was not in 'planning' (nothing is written then).
    """
    updated = await conn.fetchval(
        """
        UPDATE goals SET status = 'planned', plan_source = $2, planned_at = now()
        WHERE id = $1 AND status = 'planning' RETURNING id
        """,
        goal_id,
        source,
    )
    if updated is None:
        return False

    # The planner's ids ("t1") become real UUIDs; depends_on is translated to match.
    ids = {t.id: uuid4() for t in plan.tasks}
    await add_event(
        conn,
        "goal.planned",
        {"goal_id": str(goal_id), "source": source, "task_count": len(plan.tasks)},
    )
    for t in plan.tasks:
        depends_on = [ids[d] for d in t.depends_on]
        await conn.execute(
            """
            INSERT INTO tasks (id, goal_id, key, name, description, depends_on, needs_approval)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            ids[t.id],
            goal_id,
            t.id,
            t.name,
            t.description,
            depends_on,
            t.needs_approval,
        )
        await add_event(
            conn,
            "task.created",
            {
                "goal_id": str(goal_id),
                "task_id": str(ids[t.id]),
                "key": t.id,
                "name": t.name,
                "depends_on": [str(d) for d in depends_on],
                "needs_approval": t.needs_approval,
            },
        )
    await advance_goal(conn, goal_id)  # start the tasks that have no dependencies
    return True


# --- running a goal (step 2) --------------------------------------------------------------------
#
# Task statuses: pending -> (awaiting_approval ->) queued -> succeeded | failed
#                pending/awaiting_approval -> rejected | cancelled
# Goal statuses: planning -> planned -> running -> succeeded | failed
#
# Every function here runs inside the caller's transaction and writes its events there. Functions
# that decide what to enqueue take the goal row lock first (FOR UPDATE), so two sibling tasks
# finishing at the same moment take turns and each dependent is enqueued exactly once.


def task_idempotency_key(task_id: UUID) -> str:
    return f"task:{task_id}"


def build_task_prompt(goal_prompt: str, task: asyncpg.Record, deps: list[asyncpg.Record]) -> str:
    """The text sent to the provider: the goal, this task, and the outputs of its dependencies."""
    lines = [f"Goal: {goal_prompt}", f"Task: {task['name']}. {task['description']}"]
    if deps:
        lines.append("Results of the tasks this one depends on:")
        for d in deps:
            output = json.loads(d["output"]) if d["output"] else {}
            lines.append(f"- {d['name']}: {output.get('text', '')}")
    return "\n".join(lines)


async def _lock_goal(conn: asyncpg.Connection, goal_id: UUID) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT status, prompt FROM goals WHERE id = $1 FOR UPDATE", goal_id)


async def enqueue_task(conn: asyncpg.Connection, goal_id: UUID, task_id: UUID) -> bool:
    """Create the task's job (+ outbox row) and mark the task queued. Caller holds the goal lock.

    Returns False if the task was no longer waiting to be queued. The unique idempotency key on
    the job is a second guard: a task can never get two jobs.
    """
    goal = await conn.fetchrow("SELECT prompt FROM goals WHERE id = $1", goal_id)
    task = await conn.fetchrow(
        "SELECT id, name, description, depends_on, status FROM tasks WHERE id = $1", task_id
    )
    if task is None or task["status"] not in ("pending", "awaiting_approval"):
        return False
    deps = await conn.fetch(
        "SELECT name, output FROM tasks WHERE id = ANY($1::uuid[]) ORDER BY key", task["depends_on"]
    )
    payload = {
        "goal_id": str(goal_id),
        "task_id": str(task_id),
        "prompt": build_task_prompt(goal["prompt"], task, deps),
    }
    job_id = await conn.fetchval(
        """
        INSERT INTO jobs (task_id, type, payload, idempotency_key)
        VALUES ($1, 'task', $2::jsonb, $3)
        ON CONFLICT (idempotency_key) DO NOTHING RETURNING id
        """,
        task_id,
        json.dumps(payload),
        task_idempotency_key(task_id),
    )
    if job_id is None:
        return False
    await conn.execute("INSERT INTO outbox (job_id) VALUES ($1)", job_id)
    await conn.execute("UPDATE tasks SET status = 'queued' WHERE id = $1", task_id)
    await add_event(
        conn,
        "task.queued",
        {"goal_id": str(goal_id), "task_id": str(task_id), "job_id": str(job_id)},
    )
    return True


async def advance_goal(conn: asyncpg.Connection, goal_id: UUID) -> None:
    """Start every pending task whose dependencies have all succeeded; finish the goal when every
    task has. Safe to call any number of times: it only acts on tasks still 'pending'."""
    goal = await _lock_goal(conn, goal_id)
    if goal is None or goal["status"] not in ("planned", "running"):
        return
    tasks = await conn.fetch(
        """
        SELECT id, key, name, depends_on, needs_approval, status, output
        FROM tasks WHERE goal_id = $1 ORDER BY key
        """,
        goal_id,
    )
    by_id = {t["id"]: t for t in tasks}

    if all(t["status"] == "succeeded" for t in tasks):
        result = {
            "tasks": [{"key": t["key"], "name": t["name"], "output": json.loads(t["output"])}
                      for t in tasks]
        }  # fmt: skip
        await conn.execute(
            "UPDATE goals SET status = 'succeeded', result = $2::jsonb WHERE id = $1",
            goal_id,
            json.dumps(result),
        )
        await add_event(conn, "goal.succeeded", {"goal_id": str(goal_id)})
        return

    if goal["status"] == "planned":
        await conn.execute("UPDATE goals SET status = 'running' WHERE id = $1", goal_id)
        await add_event(conn, "goal.started", {"goal_id": str(goal_id)})

    for t in tasks:
        if t["status"] != "pending":
            continue
        if any(by_id[d]["status"] != "succeeded" for d in t["depends_on"]):
            continue
        if t["needs_approval"]:
            await conn.execute(
                "UPDATE tasks SET status = 'awaiting_approval' WHERE id = $1", t["id"]
            )
            await add_event(
                conn,
                "task.awaiting_approval",
                {"goal_id": str(goal_id), "task_id": str(t["id"]), "name": t["name"]},
            )
        else:
            await enqueue_task(conn, goal_id, t["id"])


async def complete_task(conn: asyncpg.Connection, task_id: UUID, output: dict) -> None:
    """Called from the task job's completion transaction (only for the winning delivery)."""
    goal_id = await conn.fetchval(
        """
        UPDATE tasks SET status = 'succeeded', output = $2::jsonb
        WHERE id = $1 AND status = 'queued' RETURNING goal_id
        """,
        task_id,
        json.dumps(output),
    )
    if goal_id is None:
        return  # already completed: a redelivery must not enqueue dependents again
    await add_event(conn, "task.succeeded", {"goal_id": str(goal_id), "task_id": str(task_id)})
    await advance_goal(conn, goal_id)


async def fail_goal(conn: asyncpg.Connection, goal_id: UUID, reason: str) -> None:
    """Fail the goal and cancel everything that has not started."""
    goal = await _lock_goal(conn, goal_id)
    if goal is None or goal["status"] in ("succeeded", "failed"):
        return
    await conn.execute(
        "UPDATE goals SET status = 'failed', error = $2 WHERE id = $1", goal_id, reason
    )
    await add_event(conn, "goal.failed", {"goal_id": str(goal_id), "reason": reason})
    cancelled = await conn.fetch(
        """
        UPDATE tasks SET status = 'cancelled'
        WHERE goal_id = $1 AND status IN ('pending', 'awaiting_approval') RETURNING id
        """,
        goal_id,
    )
    for t in cancelled:
        await add_event(conn, "task.cancelled", {"goal_id": str(goal_id), "task_id": str(t["id"])})


async def job_died(
    conn: asyncpg.Connection, job_type: str, payload: dict, task_id: UUID | None, error: str
) -> None:
    """A job went to the DLQ. A dead task or planner job means the goal cannot finish."""
    if job_type == "task" and task_id is not None:
        goal_id = await conn.fetchval(
            "UPDATE tasks SET status = 'failed' WHERE id = $1 AND status = 'queued' "
            "RETURNING goal_id",
            task_id,
        )
        if goal_id is not None:
            await add_event(conn, "task.failed", {"goal_id": str(goal_id), "task_id": str(task_id)})
            await fail_goal(conn, goal_id, f"task failed: {error}")
    elif job_type == "planner":
        await fail_goal(conn, UUID(payload["goal_id"]), f"planning failed: {error}")


class NotAwaitingApproval(Exception):
    pass


async def approve_task(conn: asyncpg.Connection, task_id: UUID) -> None:
    goal_id = await conn.fetchval("SELECT goal_id FROM tasks WHERE id = $1", task_id)
    if goal_id is None:
        raise LookupError("task not found")
    await _lock_goal(conn, goal_id)
    status = await conn.fetchval("SELECT status FROM tasks WHERE id = $1", task_id)
    if status != "awaiting_approval":
        raise NotAwaitingApproval(status)
    await add_event(conn, "task.approved", {"goal_id": str(goal_id), "task_id": str(task_id)})
    await enqueue_task(conn, goal_id, task_id)


async def reject_task(conn: asyncpg.Connection, task_id: UUID) -> None:
    goal_id = await conn.fetchval("SELECT goal_id FROM tasks WHERE id = $1", task_id)
    if goal_id is None:
        raise LookupError("task not found")
    await _lock_goal(conn, goal_id)
    updated = await conn.fetchval(
        "UPDATE tasks SET status = 'rejected' WHERE id = $1 AND status = 'awaiting_approval' "
        "RETURNING id",
        task_id,
    )
    if updated is None:
        raise NotAwaitingApproval(
            await conn.fetchval("SELECT status FROM tasks WHERE id = $1", task_id)
        )
    await add_event(conn, "task.rejected", {"goal_id": str(goal_id), "task_id": str(task_id)})
    await fail_goal(conn, goal_id, "task rejected")
