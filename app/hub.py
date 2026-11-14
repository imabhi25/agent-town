"""WebSocket fan-out: one reader on the Redis "events" stream, one small queue per client.

Why per-client queues: the hub never awaits a client's socket. Each client has its own sender task
that drains its own bounded queue, so a slow client can only hurt itself. If it falls too far
behind its oldest events are dropped (counted in `dropped`, so the client knows to refetch /state);
if a single send takes longer than `send_timeout` the client is disconnected.

Rate: a client's sender sends a batch as soon as something is pending, then waits `min_interval`
before the next one, so a client gets at most ~15 messages per second however many events arrive.
"""

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

import asyncpg
from redis.asyncio import Redis

from app.event_publisher import EVENTS_STREAM, parse_event
from app.stats import compute_stats

log = logging.getLogger("hub")

SNAPSHOT_EVENTS = 100
SNAPSHOT_GOALS = 20
MAX_BATCH = 200
MAX_PENDING = 1000
MAX_FPS = 15


class Client:
    def __init__(
        self,
        send: Callable[[dict], Awaitable[None]],
        close: Callable[[], Awaitable[None]] | None,
        join_id: str,
        min_interval: float,
        send_timeout: float,
        max_pending: int,
    ):
        self.send, self.close, self.join_id = send, close, join_id
        self.min_interval, self.send_timeout = min_interval, send_timeout
        self.pending: deque[dict] = deque(maxlen=max_pending)
        self.dropped = 0
        self.wakeup = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.alive = True

    def push(self, event: dict) -> None:
        if len(self.pending) == self.pending.maxlen:
            self.dropped += 1  # deque drops the oldest on append
        self.pending.append(event)
        self.wakeup.set()

    def start(self) -> None:
        self.task = asyncio.create_task(self._sender(), name="ws-sender")

    async def _sender(self) -> None:
        try:
            while True:
                await self.wakeup.wait()
                self.wakeup.clear()
                if not self.pending:
                    continue
                batch = [self.pending.popleft() for _ in range(min(MAX_BATCH, len(self.pending)))]
                if self.pending:
                    self.wakeup.set()
                message = {"type": "events", "events": batch}
                if self.dropped:
                    message["dropped"], self.dropped = self.dropped, 0
                await asyncio.wait_for(self.send(message), timeout=self.send_timeout)
                await asyncio.sleep(self.min_interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Slow (send timed out) or gone (send raised): drop this client only.
            log.warning("dropping websocket client", exc_info=True)
            self.alive = False
            if self.close is not None:
                with contextlib.suppress(Exception):
                    await self.close()

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class EventHub:
    def __init__(
        self,
        redis: Redis,
        fps: int = MAX_FPS,
        send_timeout: float = 5.0,
        max_pending: int = MAX_PENDING,
        block_ms: int = 500,
        idle_mode: bool = False,
    ):
        self.redis = redis
        self.block_ms = block_ms
        # idle_mode (live demo): read the stream only while someone is connected.
        self.idle_mode = idle_mode
        self.idle = idle_mode
        self._wake = asyncio.Event()
        self.min_interval = 1.0 / fps
        self.send_timeout, self.max_pending = send_timeout, max_pending
        self.clients: set[Client] = set()
        self.last_id = "0-0"

    async def init(self) -> None:
        """Start reading after the newest event; older ones reach clients via the snapshot."""
        latest = await self.redis.xrevrange(EVENTS_STREAM, count=1)
        self.last_id = latest[0][0] if latest else "0-0"

    async def ensure_active(self) -> None:
        """Call before register(). If the hub was idle (nobody connected) its position is stale,
        so jump to the newest event; older ones reach the new client through the snapshot."""
        if self.idle_mode and self.idle:
            await self.init()
            self.idle = False
            self._wake.set()

    def register(
        self, send: Callable[[dict], Awaitable[None]], close: Callable[[], Awaitable[None]] | None
    ) -> Client:
        """Synchronous on purpose: no await between reading last_id and subscribing, so the
        snapshot (events up to join_id) and the live feed (events after it) neither overlap nor
        leave a gap."""
        client = Client(
            send, close, self.last_id, self.min_interval, self.send_timeout, self.max_pending
        )
        self.clients.add(client)
        return client

    async def unregister(self, client: Client) -> None:
        self.clients.discard(client)
        await client.stop()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self.idle_mode and not self.clients:
                self.idle = True
                self._wake.clear()
                waiters = [asyncio.create_task(self._wake.wait()), asyncio.create_task(stop.wait())]
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                for w in waiters:
                    w.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
                continue
            try:
                resp = await self.redis.xread(
                    {EVENTS_STREAM: self.last_id}, count=500, block=self.block_ms
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event hub read failed; retrying")
                await asyncio.sleep(0.5)
                continue
            for _stream, entries in resp or []:
                for stream_id, fields in entries:
                    self.last_id = stream_id
                    event = parse_event(stream_id, fields)
                    for client in list(self.clients):
                        client.push(event)
            # Clients whose sender gave up are removed here, never blocking the reader.
            for client in [c for c in self.clients if not c.alive]:
                self.clients.discard(client)


def _json(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row(record: asyncpg.Record, *fields: str) -> dict:
    return {f: _json(record[f]) for f in fields}


async def build_snapshot(
    pool: asyncpg.Pool, redis: Redis, up_to: str = "+", worker_ids: list[str] | None = None
) -> dict:
    """Everything a client needs to draw the world: goals, tasks, workers, queue depth and the
    last 100 events (oldest first, up to the stream id `up_to`)."""
    goal_rows = await pool.fetch(
        """
        SELECT id, prompt, status, plan_source, error, created_at
        FROM goals ORDER BY created_at DESC, id LIMIT $1
        """,
        SNAPSHOT_GOALS,
    )
    goal_ids = [g["id"] for g in goal_rows]
    task_rows = await pool.fetch(
        """
        SELECT id, goal_id, key, name, description, depends_on, needs_approval, status
        FROM tasks WHERE goal_id = ANY($1::uuid[]) ORDER BY goal_id, key
        """,
        goal_ids,
    )
    counts = {
        r["status"]: r["n"]
        for r in await pool.fetch("SELECT status, count(*) AS n FROM jobs GROUP BY status")
    }
    running = {
        r["locked_by"]: str(r["id"])
        for r in await pool.fetch("SELECT id, locked_by FROM jobs WHERE status = 'running'")
    }
    if worker_ids is None:
        worker_ids = sorted([k.split(":", 2)[2] async for k in redis.scan_iter("worker:hb:*")])
    entries = await redis.xrevrange(EVENTS_STREAM, max=up_to, count=SNAPSHOT_EVENTS)
    stats = await compute_stats(pool, redis, worker_ids=worker_ids)
    return {
        "type": "snapshot",
        "goals": [
            _row(g, "id", "prompt", "status", "plan_source", "error", "created_at")
            for g in goal_rows
        ],
        "tasks": [
            {
                **_row(
                    t, "id", "goal_id", "key", "name", "description", "needs_approval", "status"
                ),
                "depends_on": [str(d) for d in t["depends_on"]],
            }
            for t in task_rows
        ],
        "workers": [{"id": w, "job_id": running.get(w)} for w in worker_ids],
        "queue": {
            "depth": counts.get("queued", 0) + counts.get("retrying", 0),
            "queued": counts.get("queued", 0),
            "retrying": counts.get("retrying", 0),
            "running": counts.get("running", 0),
            "succeeded": counts.get("succeeded", 0),
            "dead": counts.get("dead", 0),
        },
        "stats": stats,
        "events": [parse_event(sid, f) for sid, f in reversed(entries)],
        "last_event_id": up_to if up_to != "+" else (entries[0][0] if entries else "0-0"),
    }
