"""Creating jobs, with one rule about the provider-control fields in the payload.

Payload fields starting with `mock_` (mock_mode, mock_latency, ...) steer the mock provider: they
can make a worker fail on purpose or sleep. Anyone who can POST /jobs could abuse them, so the
public endpoint rejects them, except `mock_latency`, which is clamped to 10 seconds. Trusted
callers (the chaos endpoints, tests) pass trusted=True and may set any of them; latency is still
clamped, as the worker does too.
"""

import json

import asyncpg

MAX_MOCK_LATENCY = 10.0


class MockFieldRejected(ValueError):
    pass


def clamp_latency(value) -> float:
    return min(max(float(value), 0.0), MAX_MOCK_LATENCY)


def sanitize_payload(payload: dict, trusted: bool) -> dict:
    clean = dict(payload)
    for key in [k for k in clean if k.startswith("mock_")]:
        if key == "mock_latency":
            try:
                clean[key] = clamp_latency(clean[key])
            except (TypeError, ValueError):
                raise MockFieldRejected("mock_latency must be a number") from None
        elif not trusted:
            raise MockFieldRejected(f"{key} is not accepted on this endpoint")
    return clean


async def create_job(
    conn: asyncpg.Connection,
    type_: str,
    payload: dict,
    idempotency_key: str,
    max_attempts: int = 5,
    trusted: bool = False,
) -> tuple[asyncpg.Record | None, asyncpg.Record | None]:
    """Insert a job and its outbox row (call inside a transaction).

    Returns (created, existing): `created` has the new id; `existing` is the job that already
    used this idempotency key.
    """
    payload = sanitize_payload(payload, trusted)
    row = await conn.fetchrow(
        """
        INSERT INTO jobs (type, payload, idempotency_key, max_attempts)
        VALUES ($1, $2::jsonb, $3, $4)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        type_,
        json.dumps(payload),
        idempotency_key,
        max_attempts,
    )
    if row is None:
        existing = await conn.fetchrow(
            "SELECT id, status FROM jobs WHERE idempotency_key = $1", idempotency_key
        )
        return None, existing
    await conn.execute("INSERT INTO outbox (job_id) VALUES ($1)", row["id"])
    return row, None
