"""Events outbox -> Redis stream -> hub -> WebSocket. Real Postgres and Redis; fake sockets only
where the test needs to control how slow a client is."""

import asyncio
import itertools
import json
import time

import pytest
from helpers import _wait_for

from app.event_publisher import EVENTS_STREAM, publish_events
from app.events import add_event
from app.hub import EventHub, build_snapshot


async def _add(pool, n: int, start: int = 0, type_: str = "test.event") -> None:
    async with pool.acquire() as conn, conn.transaction():
        for i in range(start, start + n):
            await add_event(conn, type_, {"n": i})


async def _stream_ids(redis) -> list[int]:
    return [int(f["id"]) for _, f in await redis.xrange(EVENTS_STREAM)]


@pytest.fixture
async def api(client, redis):
    from app.main import app

    app.state.redis = redis
    return client


# --- publisher: none lost, in order ---------------------------------------------------------------


async def test_publisher_copies_every_event_in_id_order_once(pool, redis):
    await _add(pool, 30)
    assert await publish_events(pool, redis, batch_size=12) == 12  # batches, still in order
    assert await publish_events(pool, redis, batch_size=12) == 12
    assert await publish_events(pool, redis, batch_size=12) == 6
    assert await publish_events(pool, redis) == 0  # nothing left, nothing republished
    ids = await _stream_ids(redis)
    assert len(ids) == 30 and ids == sorted(ids)
    assert await pool.fetchval("SELECT count(*) FROM events WHERE published_at IS NULL") == 0


async def test_failed_publish_loses_nothing(pool, redis, monkeypatch):
    await _add(pool, 10)
    real_xadd = redis.xadd
    calls = 0

    async def flaky_xadd(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 6:
            raise ConnectionError("redis went away mid-batch")
        return await real_xadd(*args, **kwargs)

    monkeypatch.setattr(redis, "xadd", flaky_xadd)
    with pytest.raises(ConnectionError):
        await publish_events(pool, redis)
    # The transaction rolled back: nothing is marked published, so everything is retried.
    assert await pool.fetchval("SELECT count(*) FROM events WHERE published_at IS NULL") == 10
    monkeypatch.undo()
    assert await publish_events(pool, redis) == 10
    ids = set(await _stream_ids(redis))
    assert len(ids) == 10  # every event is in the stream (the 5 sent before the failure are
    # duplicated: at-least-once, consumers dedupe by id)


async def test_only_one_publisher_runs_at_a_time(pool, redis):
    await _add(pool, 5)
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        from app.event_publisher import PUBLISHER_LOCK

        assert await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", PUBLISHER_LOCK)
        assert await publish_events(pool, redis) == 0  # lock held elsewhere: skip, keep order
        await tx.rollback()
    assert await publish_events(pool, redis) == 5


async def test_domain_events_reach_the_stream(api, pool, redis):
    await api.post("/goals", json={"prompt": "x"})
    await publish_events(pool, redis)
    entries = await redis.xrange(EVENTS_STREAM)
    assert [f["type"] for _, f in entries] == ["goal.created"]


# --- snapshot ---------------------------------------------------------------------------------


async def test_snapshot_content(api, pool, redis):
    goal_id = (await api.post("/goals", json={"prompt": "launch a newsletter"})).json()["id"]
    async with pool.acquire() as conn, conn.transaction():
        from uuid import UUID

        from app.goals import store_plan
        from app.planner import MockPlanner

        plan = (await MockPlanner().plan("launch a newsletter")).plan
        await store_plan(conn, UUID(goal_id), plan, "mock")
    await pool.execute(
        "UPDATE jobs SET status = 'running', locked_by = 'worker-2' WHERE type = 'task'"
    )
    await redis.set("worker:hb:worker-1", "1", ex=30)
    await redis.set("worker:hb:worker-2", "1", ex=30)
    await publish_events(pool, redis)

    snap = (await api.get("/state")).json()
    assert snap["type"] == "snapshot"
    assert [g["id"] for g in snap["goals"]] == [goal_id]
    assert snap["goals"][0]["status"] == "running"
    assert [t["key"] for t in snap["tasks"]] == ["t1", "t2", "t3", "t4"]
    assert snap["tasks"][0]["status"] == "queued"  # t1's job exists; its job row is 'running'
    assert snap["workers"] == [
        {"id": "worker-1", "job_id": None},
        {"id": "worker-2", "job_id": snap["workers"][1]["job_id"]},
    ]
    assert snap["workers"][1]["job_id"] is not None
    assert snap["queue"]["running"] == 1 and snap["queue"]["queued"] == 1  # planner + task jobs
    types = [e["type"] for e in snap["events"]]
    assert types[0] == "goal.created" and types.count("task.created") == 4
    ids = [e["id"] for e in snap["events"]]
    assert ids == sorted(ids)  # oldest first
    assert snap["last_event_id"] == snap["events"][-1]["stream_id"]


async def test_snapshot_returns_only_the_last_100_events_oldest_first(pool, redis):
    await _add(pool, 130)
    await publish_events(pool, redis, batch_size=500)
    snap = await build_snapshot(pool, redis)
    assert len(snap["events"]) == 100
    assert [e["payload"]["n"] for e in snap["events"]] == list(range(30, 130))


# --- hub: batching, slow clients, disconnects --------------------------------------------------


async def _emit(redis, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        await redis.xadd(
            EVENTS_STREAM,
            {"id": str(i), "type": "t", "payload": json.dumps({"n": i}), "ts": "now"},
        )


class FakeSocket:
    """Records messages with arrival times. `gate` (if set) makes send wait, like a slow client."""

    def __init__(self, gate: asyncio.Event | None = None, fail: bool = False, hang: bool = False):
        self.messages: list[tuple[float, dict]] = []
        self.gate, self.fail, self.hang, self.closed = gate, fail, hang, False

    async def send(self, message: dict) -> None:
        if self.fail:
            raise ConnectionError("client went away")
        if self.hang:
            await asyncio.sleep(60)
        if self.gate is not None:
            await self.gate.wait()
        self.messages.append((time.monotonic(), message))

    async def close(self) -> None:
        self.closed = True

    def event_ids(self) -> list[int]:
        return [e["id"] for _, m in self.messages for e in m["events"]]


@pytest.fixture
async def hub_factory(redis):
    started: list[tuple[EventHub, asyncio.Event, asyncio.Task]] = []

    async def make(**kwargs) -> EventHub:
        hub = EventHub(redis, **kwargs)
        await hub.init()
        stop = asyncio.Event()
        started.append((hub, stop, asyncio.create_task(hub.run(stop))))
        return hub

    yield make
    for hub, stop, task in started:
        stop.set()
        for client in list(hub.clients):
            await client.stop()
        await asyncio.gather(task, return_exceptions=True)


def _join(hub: EventHub, sock: FakeSocket):
    client = hub.register(sock.send, sock.close)
    client.start()
    return client


async def test_events_arrive_in_order_and_batched_to_15_messages_per_second(redis, hub_factory):
    hub = await hub_factory(fps=15)
    sock = FakeSocket()
    _join(hub, sock)
    for burst in range(40):  # 240 events over about a second
        await _emit(redis, 6, start=burst * 6)
        await asyncio.sleep(0.025)
    await _wait_for(lambda: _done(sock, 240))

    assert sock.event_ids() == list(range(240))  # complete, in order, no duplicates
    times = [t for t, _ in sock.messages]
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert min(gaps) >= (1 / 15) * 0.9  # never faster than ~15 messages per second
    assert len(sock.messages) < 240 / 6  # genuinely batched: far fewer messages than bursts


async def _done(sock: FakeSocket, n: int) -> bool:
    return len(sock.event_ids()) >= n


async def test_slow_client_does_not_block_others(redis, hub_factory):
    hub = await hub_factory(send_timeout=0.3)
    slow, fast = FakeSocket(hang=True), FakeSocket()
    _join(hub, slow)
    _join(hub, fast)
    await _emit(redis, 30)
    await _wait_for(lambda: _done(fast, 30), timeout=2)
    assert fast.event_ids() == list(range(30))  # unaffected by the stuck neighbour
    # The slow client is disconnected after its send timed out, and removed from the hub.
    await _emit(redis, 1, start=30)
    await _wait_for(lambda: _gone(hub, slow), timeout=3)
    assert slow.closed


async def _gone(hub, sock) -> bool:
    return sock.closed and len(hub.clients) == 1


async def test_disconnected_client_is_removed_and_others_continue(redis, hub_factory):
    hub = await hub_factory()
    gone, stays = FakeSocket(fail=True), FakeSocket()
    _join(hub, gone)
    _join(hub, stays)
    await _emit(redis, 3)
    await _wait_for(lambda: _done(stays, 3))
    await _emit(redis, 3, start=3)
    await _wait_for(lambda: _done(stays, 6))
    await _wait_for(lambda: _is_removed(hub, gone))
    assert stays.event_ids() == list(range(6))


async def _is_removed(hub, sock) -> bool:
    return sock.closed and len(hub.clients) == 1


async def test_client_that_falls_behind_drops_oldest_and_is_told(redis, hub_factory):
    hub = await hub_factory(max_pending=5, send_timeout=10)
    gate = asyncio.Event()
    sock = FakeSocket(gate=gate)
    _join(hub, sock)
    await _emit(redis, 1)
    await asyncio.sleep(0.3)  # first batch is now stuck in send()
    await _emit(redis, 20, start=1)
    await asyncio.sleep(0.3)
    gate.set()
    await _wait_for(lambda: _done(sock, 6))
    last = sock.messages[-1][1]
    assert last["dropped"] == 15  # told how many were skipped, so it can refetch /state
    assert [e["id"] for e in last["events"]] == [16, 17, 18, 19, 20]  # the newest five survive


async def test_client_joining_mid_stream_gets_no_gap_and_no_overlap(pool, redis, hub_factory):
    await _add(pool, 10)
    await publish_events(pool, redis)
    hub = await hub_factory()
    sock = FakeSocket()
    client = hub.register(sock.send, sock.close)
    snapshot = await build_snapshot(pool, redis, up_to=client.join_id)
    client.start()
    await _add(pool, 5, start=10)
    await publish_events(pool, redis)
    await _wait_for(lambda: _done(sock, 5))
    snap_ns = [e["payload"]["n"] for e in snapshot["events"]]
    live_ns = [e["payload"]["n"] for _, m in sock.messages for e in m["events"]]
    assert snap_ns == list(range(10)) and live_ns == list(range(10, 15))


# --- the real endpoint ---------------------------------------------------------------------------


@pytest.fixture
async def live_server(pool, redis):
    import uvicorn

    from app.main import app

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    await _wait_for(lambda: _started(server))
    yield server.servers[0].sockets[0].getsockname()[1]
    server.should_exit = True
    await task


async def _started(server) -> bool:
    return server.started


async def test_websocket_sends_snapshot_then_live_events(live_server):
    import httpx
    from websockets.asyncio.client import connect

    async with connect(f"ws://127.0.0.1:{live_server}/ws") as ws:
        snapshot = json.loads(await asyncio.wait_for(ws.recv(), 3))
        assert snapshot["type"] == "snapshot"
        assert snapshot["goals"] == [] and snapshot["events"] == []

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{live_server}") as http:
            goal_id = (await http.post("/goals", json={"prompt": "hello"})).json()["id"]
        message = json.loads(await asyncio.wait_for(ws.recv(), 3))
        assert message["type"] == "events"
        event = message["events"][0]
        assert event["type"] == "goal.created" and event["payload"]["goal_id"] == goal_id

    # A second client connecting later gets that event in its snapshot.
    async with connect(f"ws://127.0.0.1:{live_server}/ws") as ws2:
        snap2 = json.loads(await asyncio.wait_for(ws2.recv(), 3))
        assert [g["id"] for g in snap2["goals"]] == [goal_id]
        assert [e["type"] for e in snap2["events"]] == ["goal.created"]
