"""Chaos endpoints: break things on purpose and watch the system recover.

Mock jobs only. These act on the worker pool inside the API process, so they need
EMBEDDED_WORKERS=true (always on in the live demo); with workers in a separate process they
answer 409. Each action writes an event so the map and the log show what happened.
"""

import random

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.events import add_event
from app.jobs import MAX_MOCK_LATENCY, clamp_latency
from app.loadgen import submit_mock_jobs
from app.providers.mock_llm import MockLLM
from app.worker import WorkerPool

router = APIRouter(prefix="/chaos", tags=["chaos"])


class KillIn(BaseModel):
    worker_id: str | None = None  # omit for a random worker


class StartIn(BaseModel):
    worker_id: str | None = None  # omit for a fresh id


class MockConfigIn(BaseModel):
    failure_rate: float | None = Field(default=None, ge=0, le=1)
    min_latency: float | None = Field(default=None, ge=0)
    max_latency: float | None = Field(default=None, ge=0)


class FloodIn(BaseModel):
    count: int = Field(ge=1)
    latency: float | None = Field(default=None, ge=0)  # seconds each job takes (max 10)


async def _guard(request: Request) -> WorkerPool:
    guard = getattr(request.app.state, "guard", None)
    if guard is not None:
        denial = await guard.check_chaos(request)
        if denial is not None:
            status, message, headers = denial
            raise HTTPException(status_code=status, detail=message, headers=headers)
    workers = getattr(request.app.state, "workers", None)
    if workers is None:
        raise HTTPException(
            status_code=409, detail="Workers are not running inside the API (EMBEDDED_WORKERS)."
        )
    return workers


async def _event(request: Request, type_: str, payload: dict) -> None:
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        await add_event(conn, type_, payload)


def _mock(workers: WorkerPool) -> MockLLM:
    return getattr(workers.provider, "mock", workers.provider)


@router.get("/status")
async def status(request: Request):
    workers = await _guard(request)
    mock = _mock(workers)
    return {
        "workers": sorted(workers.tasks),
        "failure_rate": mock.failure_rate,
        "min_latency": mock.min_latency,
        "max_latency": mock.max_latency,
        "flood_max": settings.flood_max,
    }


@router.post("/kill")
async def kill(body: KillIn, request: Request):
    workers = await _guard(request)
    if body.worker_id is None:
        if not workers.tasks:
            raise HTTPException(status_code=404, detail="no workers are running")
        worker_id = random.choice(sorted(workers.tasks))
    else:
        worker_id = body.worker_id
        if worker_id not in workers.tasks:
            raise HTTPException(status_code=404, detail=f"no running worker {worker_id}")
    await workers.kill_worker(worker_id)  # a crash: its message stays pending for recovery
    await _event(request, "worker.killed", {"worker": worker_id})
    return {"killed": worker_id, "workers": sorted(workers.tasks)}


@router.post("/start")
async def start(body: StartIn, request: Request):
    workers = await _guard(request)
    worker_id = body.worker_id or workers.new_worker_id()
    if worker_id in workers.tasks:
        raise HTTPException(status_code=409, detail=f"worker {worker_id} is already running")
    await workers.start_worker(worker_id)
    await _event(request, "worker.started", {"worker": worker_id})
    return {"started": worker_id, "workers": sorted(workers.tasks)}


@router.post("/mock")
async def configure_mock(body: MockConfigIn, request: Request):
    workers = await _guard(request)
    mock = _mock(workers)
    low = mock.min_latency if body.min_latency is None else clamp_latency(body.min_latency)
    high = mock.max_latency if body.max_latency is None else clamp_latency(body.max_latency)
    if low > high:
        raise HTTPException(status_code=400, detail="min_latency must not exceed max_latency")
    if body.failure_rate is not None:
        mock.failure_rate = body.failure_rate
    mock.min_latency, mock.max_latency = low, high
    config = {
        "failure_rate": mock.failure_rate,
        "min_latency": mock.min_latency,
        "max_latency": mock.max_latency,
    }
    await _event(request, "chaos.config", config)
    return config


@router.post("/flood")
async def flood(body: FloodIn, request: Request):
    await _guard(request)
    if body.count > settings.flood_max:
        raise HTTPException(
            status_code=400, detail=f"A flood is limited to {settings.flood_max} jobs here."
        )
    latency = None if body.latency is None else min(body.latency, MAX_MOCK_LATENCY)
    pool: asyncpg.Pool = request.app.state.pool
    await submit_mock_jobs(pool, body.count, "flood", latency=latency)
    await _event(request, "chaos.flood", {"count": body.count})
    return {"created": body.count}
