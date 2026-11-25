"""Deployment safety: hosted-database pool settings, migrations on an empty database, the
live-demo configuration checks and the WebSocket origin check."""

import re
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings, production_problems, settings
from app.db import clean_url, is_pooled, pool_kwargs
from app.migrate import MIGRATIONS, UnmanagedDatabase, migrate

ROOT = Path(__file__).resolve().parent.parent

# ---- pool settings for Neon ----


def test_direct_host_keeps_prepared_statements():
    kw = pool_kwargs(
        "postgresql://USER:PASSWORD@ep-cool-123.eu-central-1.aws.neon.tech/db?sslmode=require"
    )
    assert "statement_cache_size" not in kw


def test_pooler_host_turns_the_statement_cache_off():
    url = "postgresql://USER:PASSWORD@ep-cool-123-pooler.eu-central-1.aws.neon.tech/db?sslmode=require"
    assert is_pooled(url)
    assert pool_kwargs(url)["statement_cache_size"] == 0


def test_a_pooler_can_be_forced_for_other_providers():
    assert (
        pool_kwargs("postgresql://USER:PASSWORD@db.example.com/x", forced_pooler=True)[
            "statement_cache_size"
        ]
        == 0
    )


def test_idle_connections_are_dropped_quickly_so_a_sleeping_database_is_not_a_problem():
    assert pool_kwargs("postgresql://USER:PASSWORD@h/db")["max_inactive_connection_lifetime"] <= 60


# ---- migrations ----


@pytest.fixture
async def scratch_db():
    """An empty database of its own, so the real one is untouched."""
    admin_url = settings.database_url
    name = "agt_migrate_test"
    try:
        admin = await asyncpg.connect(admin_url)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.skip(f"Postgres not available: {e}")
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {name}")
    except asyncpg.InsufficientPrivilegeError:
        await admin.close()
        pytest.skip("the test role cannot create databases")
    parsed = urlparse(admin_url)
    pool = await asyncpg.create_pool(
        parsed._replace(path=f"/{name}").geturl(), min_size=1, max_size=3
    )
    yield pool
    await pool.close()
    await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    await admin.close()


async def test_migrate_builds_the_schema_on_an_empty_database(scratch_db):
    applied = await migrate(scratch_db)
    assert applied == sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    tables = {
        r["tablename"]
        for r in await scratch_db.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    }
    assert {
        "goals",
        "tasks",
        "jobs",
        "job_outputs",
        "outbox",
        "events",
        "schema_migrations",
    } <= tables


async def test_migrate_is_idempotent(scratch_db):
    await migrate(scratch_db)
    assert await migrate(scratch_db) == []  # a second start applies nothing and changes nothing


async def test_a_new_migration_is_applied_alone(scratch_db, tmp_path):
    await migrate(scratch_db)
    extra = tmp_path / "900_extra.sql"
    extra.write_text("CREATE TABLE extra_table (id int);")
    # Same history table, a directory holding the old files plus one new one.
    for f in MIGRATIONS.glob("*.sql"):
        (tmp_path / f.name).write_text(f.read_text())
    assert await migrate(scratch_db, tmp_path) == ["900_extra.sql"]


async def test_a_failing_migration_leaves_nothing_half_applied(scratch_db, tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE t1 (id int);")
    (tmp_path / "002_bad.sql").write_text("CREATE TABLE t2 (id int); SELECT 1/0;")
    with pytest.raises(asyncpg.PostgresError):
        await migrate(scratch_db, tmp_path)
    assert await scratch_db.fetchval("SELECT to_regclass('t1') IS NOT NULL")  # 001 stays applied
    assert not await scratch_db.fetchval("SELECT to_regclass('t2') IS NOT NULL")  # 002 rolled back
    assert [r["name"] for r in await scratch_db.fetch("SELECT name FROM schema_migrations")] == [
        "001_a.sql"
    ]


async def test_a_database_with_tables_but_no_history_is_refused(scratch_db):
    await scratch_db.execute("CREATE TABLE goals (id int)")
    with pytest.raises(UnmanagedDatabase):
        await migrate(scratch_db)


# ---- live-demo configuration ----

GOOD = {"cors_origins": "https://agent-town.vercel.app", "trust_forwarded_for": True}


def test_a_correct_live_config_has_no_problems():
    assert production_problems(Settings(live_demo=True, **GOOD)) == []


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"cors_origins": "*"}, "must not be '*'"),
        ({"cors_origins": ""}, "empty"),
        ({"cors_origins": "http://localhost:5173"}, "https://"),
        ({"cors_origins": "https://localhost:5173"}, "localhost"),
        ({"cors_origins": "https://agent-town.vercel.app/"}, "trailing slash"),
        ({"cors_origins": "http://agent-town.vercel.app"}, "https://"),
        ({"trust_forwarded_for": False}, "TRUST_FORWARDED_FOR"),
    ],
)
def test_unsafe_live_configs_are_reported(override, fragment):
    problems = production_problems(Settings(live_demo=True, **{**GOOD, **override}))
    assert any(fragment in p for p in problems), problems


async def test_the_app_refuses_to_start_with_an_unsafe_live_config(monkeypatch):
    from app.main import app

    monkeypatch.setattr(settings, "live_demo", True)
    monkeypatch.setattr(settings, "cors_origins", "*")
    with pytest.raises(RuntimeError, match="Unsafe live-demo configuration"):
        async with app.router.lifespan_context(app):
            pass


# ---- WebSocket origin ----


def test_websocket_from_another_site_is_refused_in_the_live_demo(monkeypatch):
    from app.main import app

    monkeypatch.setattr(settings, "live_demo", True)
    monkeypatch.setattr(settings, "cors_origins", "https://agent-town.vercel.app")
    client = TestClient(app)  # no `with`: no lifespan; the check runs before any state is used
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/ws", headers={"origin": "https://evil.example"}),
    ):
        pass
    assert refused.value.code == 1008


def test_origin_rules(monkeypatch):
    from app.main import _origin_allowed

    monkeypatch.setattr(settings, "cors_origins", "https://agent-town.vercel.app")
    monkeypatch.setattr(settings, "live_demo", False)
    assert _origin_allowed("https://anything.example")  # local development: no check
    monkeypatch.setattr(settings, "live_demo", True)
    assert _origin_allowed("https://agent-town.vercel.app")
    assert not _origin_allowed("https://agent-town.vercel.app.evil.example")
    assert not _origin_allowed("http://agent-town.vercel.app")
    assert _origin_allowed(None)  # curl and tests send no Origin


# ---- Redis client timeouts (found by booting the live profile in a clean environment) ----


def test_redis_socket_timeout_exceeds_the_longest_blocking_read():
    from app.redis_budget import SOCKET_TIMEOUT, CountingRedis

    live_block_seconds = Settings(live_demo=True).worker_block_ms / 1000
    assert SOCKET_TIMEOUT >= 3 * live_block_seconds
    r = CountingRedis.from_url("redis://localhost:6379/0")
    kw = r.connection_pool.connection_kwargs
    assert kw["socket_timeout"] == SOCKET_TIMEOUT
    assert kw["health_check_interval"] == 30


@pytest.mark.slow
async def test_a_5s_blocking_read_does_not_time_out(redis):
    """The regression itself: BLOCK 5000 on an empty stream returns None, it does not raise."""
    from app.redis_budget import CountingRedis

    r = CountingRedis.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.xgroup_create("s9-test-stream", "g", id="$", mkstream=True)
        got = await r.xreadgroup("g", "c", {"s9-test-stream": ">"}, count=1, block=5000)
        assert got == []  # redis-py returns [] when nothing arrived
    finally:
        await r.delete("s9-test-stream")
        await r.aclose()


# ---- the deploy checklist cannot silently drift ----


def _env_example_names() -> set[str]:
    """Variable names in .env.example, commented-out lines included (they document the deploy)."""
    names = set()
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.lstrip("# ").strip()
        if "=" in line and line.split("=")[0].isupper():
            names.add(line.split("=")[0])
    return names


def test_env_example_lists_every_variable_render_needs():
    render_keys = set(re.findall(r"- key: (\w+)", (ROOT / "render.yaml").read_text()))
    assert render_keys, "render.yaml should list env vars"
    assert render_keys <= _env_example_names(), render_keys - _env_example_names()
    assert {"DATABASE_URL", "REDIS_URL", "GEMINI_API_KEY", "LIVE_DEMO", "CORS_ORIGINS"} <= (
        _env_example_names()
    )


def test_the_browser_bundle_source_never_reads_an_api_key():
    # Only VITE_* variables reach the browser, and the only one is the backend URL.
    for path in (ROOT / "web" / "src").rglob("*"):
        if path.suffix in {".ts", ".tsx"}:
            assert "API_KEY" not in path.read_text().upper(), path
    assert (ROOT / "web" / ".env.example").read_text().count("=") == 1


def test_clean_url_drops_neon_channel_binding():
    # Neon's connection string ends with channel_binding=require, which asyncpg would send to the
    # server as a setting and fail on. sslmode must survive.
    url = "postgresql://USER:PASSWORD@ep-x.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
    assert clean_url(url) == "postgresql://USER:PASSWORD@ep-x.us-east-2.aws.neon.tech/neondb?sslmode=require"
    assert clean_url("postgresql://USER:PASSWORD@h/db") == "postgresql://USER:PASSWORD@h/db"
