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
    return True
