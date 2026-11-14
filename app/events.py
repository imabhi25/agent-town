"""Event log helper. Events are written in the same transaction as the state change they describe."""

import json

import asyncpg


async def add_event(conn: asyncpg.Connection, type_: str, payload: dict) -> None:
    await conn.execute(
        "INSERT INTO events (type, payload) VALUES ($1, $2::jsonb)", type_, json.dumps(payload)
    )
