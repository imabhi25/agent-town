from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="Agent Town", lifespan=lifespan)


class GoalIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=500)


class JobIn(BaseModel):
    type: str = Field(min_length=1)
    payload: dict = Field(default_factory=dict)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/goals", status_code=201)
async def create_goal(body: GoalIn, request: Request):
    """Stub: stores the goal. The Gemini planner is added in week 2."""
    row = await request.app.state.pool.fetchrow(
        "INSERT INTO goals (prompt) VALUES ($1) RETURNING id", body.prompt
    )
    return {"id": str(row["id"]), "status": "planning"}


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
            INSERT INTO jobs (type, payload, idempotency_key)
            VALUES ($1, $2::jsonb, $3)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            body.type,
            _json(body.payload),
            idempotency_key,
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
