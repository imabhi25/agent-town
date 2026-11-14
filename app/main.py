import asyncio
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app import goals
from app.config import settings
from app.dispatcher import run_dispatcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    # For now the dispatcher runs inside the API process. It can move to its own process later.
    stop = asyncio.Event()
    dispatcher = asyncio.create_task(run_dispatcher(app.state.pool, app.state.redis, stop))
    try:
        yield
    finally:
        stop.set()
        await dispatcher
        await app.state.redis.aclose()
        await app.state.pool.close()


app = FastAPI(title="Agent Town", lifespan=lifespan)


class GoalIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=500)


class JobIn(BaseModel):
    type: str = Field(min_length=1)
    payload: dict = Field(default_factory=dict)
    max_attempts: int = Field(default=5, ge=1, le=10)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/goals", status_code=201)
async def create_goal(body: GoalIn, request: Request):
    """Create a goal and queue its planner job (type 'planner') in one transaction.

    A worker plans it later; the tasks appear with goal.planned / task.created events.
    """
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        goal_id = await goals.create_goal(conn, body.prompt)
    return {"id": str(goal_id), "status": "planning"}


@app.get("/goals/{goal_id}")
async def get_goal(goal_id: UUID, request: Request):
    pool: asyncpg.Pool = request.app.state.pool
    goal = await pool.fetchrow(
        "SELECT id, prompt, status, plan_source FROM goals WHERE id = $1", goal_id
    )
    if goal is None:
        raise HTTPException(status_code=404, detail="goal not found")
    tasks = await pool.fetch(
        """
        SELECT id, key, name, description, depends_on, needs_approval, status
        FROM tasks WHERE goal_id = $1 ORDER BY key
        """,
        goal_id,
    )
    return {
        "id": str(goal["id"]),
        "prompt": goal["prompt"],
        "status": goal["status"],
        "plan_source": goal["plan_source"],
        "tasks": [
            {**dict(t), "id": str(t["id"]), "depends_on": [str(d) for d in t["depends_on"]]}
            for t in tasks
        ],
    }


@app.post("/jobs", status_code=201)
async def create_job(
    body: JobIn,
    request: Request,
    idempotency_key: str | None = Header(default=None),
):
    """Create a job and its outbox row in ONE transaction.

    A repeated Idempotency-Key returns the original job instead of creating a duplicate.
    """
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header required")

    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            INSERT INTO jobs (type, payload, idempotency_key, max_attempts)
            VALUES ($1, $2::jsonb, $3, $4)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            body.type,
            _json(body.payload),
            idempotency_key,
            body.max_attempts,
        )
        if row is None:
            existing = await conn.fetchrow(
                "SELECT id, status FROM jobs WHERE idempotency_key = $1", idempotency_key
            )
            return {"id": str(existing["id"]), "status": existing["status"], "duplicate": True}

        await conn.execute("INSERT INTO outbox (job_id) VALUES ($1)", row["id"])
        return {"id": str(row["id"]), "status": "queued", "duplicate": False}


def _json(value: dict) -> str:
    import json

    return json.dumps(value)
