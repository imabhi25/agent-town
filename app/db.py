"""One place that builds the Postgres pool, so every entry point (API, worker process, scripts)
connects the same way and hosted databases (Neon) work.

Two things a free hosted Postgres needs that a local one does not:
  - TLS: put `sslmode=require` in DATABASE_URL (asyncpg understands it).
  - A pooled endpoint (Neon's "-pooler" host, PgBouncer in transaction mode) hands each query to
    a different server connection, so asyncpg's prepared-statement cache breaks
    ("prepared statement does not exist"). `statement_cache_size=0` turns the cache off. The
    simplest fix is to use Neon's DIRECT (non-pooled) host, which needs no workaround: we keep
    only a handful of connections ourselves, so we do not need a pooler.
  - Neon's copy button adds `channel_binding=require`. libpq knows it, asyncpg does not: asyncpg
    sends unknown URL parameters to the server as settings, and Postgres refuses the connection
    ("unrecognized configuration parameter"). `clean_url` drops it; TLS still comes from sslmode.
"""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import asyncpg

from app.config import settings


def is_pooled(url: str, forced: bool = False) -> bool:
    host = urlparse(url).hostname or ""
    return forced or "-pooler" in host


# libpq-only URL parameters that asyncpg would wrongly pass to the server as settings.
_LIBPQ_ONLY_PARAMS = {"channel_binding"}


def clean_url(url: str) -> str:
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in _LIBPQ_ONLY_PARAMS]
    return urlunparse(parts._replace(query=urlencode(query)))


def pool_kwargs(url: str, forced_pooler: bool = False) -> dict:
    kwargs: dict = {
        # A free database may scale to zero and drop idle connections: do not keep them for long,
        # so the pool reconnects instead of handing out a dead one.
        "max_inactive_connection_lifetime": 60.0,
        "command_timeout": 30.0,
    }
    if is_pooled(url, forced_pooler):
        kwargs["statement_cache_size"] = 0
    return kwargs


async def create_pool(
    min_size: int = 1, max_size: int = 10, url: str | None = None
) -> asyncpg.Pool:
    url = clean_url(url or settings.database_url)
    return await asyncpg.create_pool(
        url, min_size=min_size, max_size=max_size, **pool_kwargs(url, settings.db_pooler)
    )
