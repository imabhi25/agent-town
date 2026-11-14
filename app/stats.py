"""GET /stats: what the system is doing right now, computed from Postgres (jobs, events) and the
worker heartbeats in Redis.

Latency is end to end: from the job's creation to its completion, for jobs that SUCCEEDED in the
window (default 5 minutes), so it includes queueing, retries and backoff. Throughput is succeeded
jobs per second over the last minute. Percentiles use percentile_cont (linear interpolation).
"""

import asyncpg
from redis.asyncio import Redis

LATENCY_WINDOW = 300  # seconds of history for the percentiles
THROUGHPUT_WINDOW = 60  # seconds for jobs/second


async def active_worker_ids(redis: Redis) -> list[str]:
    return sorted([k.split(":", 2)[2] async for k in redis.scan_iter("worker:hb:*")])


async def compute_stats(
    pool: asyncpg.Pool,
    redis: Redis,
    latency_window: int = LATENCY_WINDOW,
    throughput_window: int = THROUGHPUT_WINDOW,
    worker_ids: list[str] | None = None,
) -> dict:
    counts = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
               count(*) FILTER (WHERE status = 'dead') AS dead,
               count(*) FILTER (WHERE status = 'running') AS running,
               count(*) FILTER (WHERE status IN ('queued', 'retrying')) AS depth
        FROM jobs
        """
    )
    retries = await pool.fetchval("SELECT count(*) FROM events WHERE type = 'job.retry_scheduled'")
    recent = await pool.fetchrow(
        """
        SELECT count(*) AS n,
               percentile_cont(ARRAY[0.5, 0.95, 0.99])
                 WITHIN GROUP (ORDER BY extract(epoch FROM completed_at - created_at)) AS pct
        FROM jobs
        WHERE status = 'succeeded' AND completed_at >= now() - make_interval(secs => $1)
        """,
        float(latency_window),
    )
    last_window = await pool.fetchval(
        """
        SELECT count(*) FROM jobs
        WHERE status = 'succeeded' AND completed_at >= now() - make_interval(secs => $1)
        """,
        float(throughput_window),
    )
    pct = recent["pct"]
    workers = await active_worker_ids(redis) if worker_ids is None else worker_ids
    return {
        "throughput_per_sec": round(last_window / throughput_window, 3),
        "completed_last_minute": last_window,
        "succeeded": counts["succeeded"],
        "retries": retries,
        "dead": counts["dead"],
        "latency_ms": {
            "p50": None if pct is None else round(pct[0] * 1000, 1),
            "p95": None if pct is None else round(pct[1] * 1000, 1),
            "p99": None if pct is None else round(pct[2] * 1000, 1),
            "samples": recent["n"],
            "window_seconds": latency_window,
        },
        "queue_depth": counts["depth"],
        "running": counts["running"],
        "active_workers": len(workers),
    }
