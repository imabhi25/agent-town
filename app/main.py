import asyncio
import json
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app import goals
from app.chaos_api import router as chaos_router
from app.config import settings
from app.dispatcher import run_dispatcher
from app.dlq import replay_dlq
from app.event_publisher import run_event_publisher
from app.events import add_event
from app.hub import EventHub, build_snapshot
from app.jobs import MockFieldRejected
from app.jobs import create_job as insert_job
from app.loadgen import run_load_generator
from app.protection import GoalGuard
from app.reconcile import reconcile
from app.redis_budget import CommandBudget, CountingRedis
from app.sleep import SleepGate, run_sleeper
from app.stats import compute_stats
from app.worker import build_worker_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    app.state.redis = CountingRedis.from_url(settings.redis_url, decode_responses=True)
    redis, pool = app.state.redis, app.state.pool

    budget = CommandBudget(redis, settings.redis_monthly_limit, settings.redis_budget_guard_ratio)
    await budget.flush()
    gate = SleepGate(enabled=settings.live_demo, idle_seconds=settings.idle_sleep_seconds)
    app.state.gate = gate
    app.state.guard = GoalGuard(redis, budget, gate)
    # Redis may have lost data while we were down: put back what Postgres still knows is pending.
    await reconcile(pool, redis)

    stop = asyncio.Event()
    poll = 0.5 if settings.live_demo else 0.2
    background = [
        asyncio.create_task(run_dispatcher(pool, redis, stop, poll, gate)),
        asyncio.create_task(run_event_publisher(pool, redis, stop, poll / 4, gate)),
        asyncio.create_task(budget.run(stop)),
        asyncio.create_task(run_sleeper(gate, stop)),
    ]
    if settings.load_generator and not settings.live_demo:
        background.append(
            asyncio.create_task(run_load_generator(pool, stop, settings.load_interval))
        )
    app.state.hub = EventHub(
        redis,
        block_ms=settings.worker_block_ms,
        idle_mode=settings.live_demo,
    )
    await app.state.hub.init()
    hub_task = asyncio.create_task(app.state.hub.run(stop))
    workers = None
    if settings.embedded_workers:
        workers = build_worker_pool(pool, redis, gate)
        app.state.workers = workers
        await workers.start()
    try:
        yield
    finally:
        stop.set()
        hub_task.cancel()  # may be blocked in XREAD
        if workers is not None:
            await workers.stop()
        await asyncio.gather(*background, hub_task, return_exceptions=True)
        await redis.aclose()
        await pool.close()


app = FastAPI(title="Agent Town", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Idempotency-Key"],
)
app.include_router(chaos_router)


class GoalIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=500)


class JobIn(BaseModel):
    type: str = Field(min_length=1)
    payload: dict = Field(default_factory=dict)
    max_attempts: int = Field(default=5, ge=1, le=10)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/healthz")
async def healthz(request: Request):
    """Liveness for hosting and for the frontend's wake-up poll. Deliberately touches neither
    Postgres nor Redis (a free host may be cold, and this must answer fast and cost nothing).
    `live` says whether live mode is open (false when the Redis command budget is nearly used)."""
    guard = getattr(request.app.state, "guard", None)
    gate = getattr(request.app.state, "gate", None)
    return {
        "status": "ok",
        "live": True if guard is None else guard.live_available,
        "sleeping": bool(gate and gate.sleeping),
    }


@app.get("/stats")
async def get_stats(request: Request):
    return await compute_stats(request.app.state.pool, request.app.state.redis)


@app.get("/state")
async def get_state(request: Request):
    """The same snapshot a WebSocket client receives on connect."""
    return await build_snapshot(request.app.state.pool, request.app.state.redis)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """Snapshot first, then batched live events (see app/hub.py)."""
    await websocket.accept()
    state = websocket.app.state
    await state.hub.ensure_active()
    client = state.hub.register(websocket.send_json, websocket.close)
    try:
        snapshot = await build_snapshot(state.pool, state.redis, up_to=client.join_id)
        await websocket.send_json(snapshot)
        client.start()
        while True:  # we only listen for the disconnect; clients send nothing
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await state.hub.unregister(client)


@app.post("/goals", status_code=201)
async def create_goal(body: GoalIn, request: Request):
    """Create a goal and queue its planner job (type 'planner') in one transaction.

    A worker plans it later; the tasks appear with goal.planned / task.created events.
    """
    guard = getattr(request.app.state, "guard", None)
    if guard is not None:
        denial = await guard.check(request)
        if denial is not None:
            status, message, headers = denial
            raise HTTPException(status_code=status, detail=message, headers=headers)
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        goal_id = await goals.create_goal(conn, body.prompt)
    return {"id": str(goal_id), "status": "planning"}


@app.post("/dlq/{job_id}/replay")
async def replay_dead_job(job_id: UUID, request: Request):
    """Give a dead job a fresh retry budget and queue it again (see app/dlq.py)."""
    guard = getattr(request.app.state, "guard", None)
    if guard is not None:  # public in the demo, so it shares the chaos per-IP limit
        denial = await guard.check_chaos(request)
        if denial is not None:
            status, message, headers = denial
            raise HTTPException(status_code=status, detail=message, headers=headers)
    if not await replay_dlq(request.app.state.pool, request.app.state.redis, job_id):
        raise HTTPException(status_code=409, detail="job is not dead, nothing to replay")
    return {"id": str(job_id), "status": "queued"}


@app.post("/tasks/{task_id}/approve")
async def approve_task(task_id: UUID, request: Request):
    return await _decide(request, task_id, goals.approve_task, "approved")


@app.post("/tasks/{task_id}/reject")
async def reject_task(task_id: UUID, request: Request):
    return await _decide(request, task_id, goals.reject_task, "rejected")


async def _decide(request: Request, task_id: UUID, action, result: str):
    pool: asyncpg.Pool = request.app.state.pool
    try:
        async with pool.acquire() as conn, conn.transaction():
            await action(conn, task_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found") from None
    except goals.NotAwaitingApproval as e:
        raise HTTPException(status_code=409, detail=f"task is {e}, not awaiting approval") from None
    return {"id": str(task_id), "status": result}


@app.get("/goals/{goal_id}")
async def get_goal(goal_id: UUID, request: Request):
    pool: asyncpg.Pool = request.app.state.pool
    goal = await pool.fetchrow(
        "SELECT id, prompt, status, plan_source, result, error FROM goals WHERE id = $1", goal_id
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
        "result": json.loads(goal["result"]) if goal["result"] else None,
        "error": goal["error"],
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
    try:
        async with pool.acquire() as conn, conn.transaction():
            created, existing = await insert_job(
                conn, body.type, body.payload, idempotency_key, body.max_attempts
            )
            if created is not None:  # the map counts queue depth from events
                await add_event(conn, "job.created", {"job_id": str(created["id"])})
    except MockFieldRejected as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    if created is None:
        return {"id": str(existing["id"]), "status": existing["status"], "duplicate": True}
    guard = getattr(request.app.state, "guard", None)
    if guard is not None:
        guard.touch()
    return {"id": str(created["id"]), "status": "queued", "duplicate": False}
