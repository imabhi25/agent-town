"""Apply migrations/*.sql in order, once each, and remember which ran.

Locally docker compose loads the files into a fresh database. A hosted database (Neon) starts
empty and we have no shell on it, so the app applies them itself at startup when AUTO_MIGRATE is
on. Each file runs in its own transaction together with its history row, under an advisory lock,
so two instances starting at once cannot both apply it and a failed file leaves nothing half done.

    python -m app.migrate        # apply now, e.g. from your laptop against Neon
"""

import asyncio
import logging
from pathlib import Path

import asyncpg

log = logging.getLogger("migrate")

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
LOCK_KEY = 7_300_002  # arbitrary constant for pg_advisory_lock


class UnmanagedDatabase(RuntimeError):
    pass


async def migrate(pool: asyncpg.Pool, directory: Path = MIGRATIONS) -> list[str]:
    """Apply every migration not applied yet. Returns the file names applied (empty = up to date)."""
    files = sorted(directory.glob("*.sql"))
    applied: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
        try:
            fresh = not await conn.fetchval(
                "SELECT to_regclass('public.schema_migrations') IS NOT NULL"
            )
            if fresh and await conn.fetchval("SELECT to_regclass('public.goals') IS NOT NULL"):
                # Tables exist but we never recorded them (loaded by hand or by docker compose).
                # Guessing which files ran could corrupt the schema: stop and say so.
                raise UnmanagedDatabase(
                    "The database already has tables but no schema_migrations history. "
                    "Use an empty database, or create the history yourself."
                )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            for f in files:
                if f.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(f.read_text())
                    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", f.name)
                log.info("applied %s", f.name)
                applied.append(f.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
    return applied


async def _main() -> None:
    from app.db import create_pool

    pool = await create_pool(max_size=2)
    try:
        applied = await migrate(pool)
        print("applied: " + (", ".join(applied) or "nothing (already up to date)"))
    finally:
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
