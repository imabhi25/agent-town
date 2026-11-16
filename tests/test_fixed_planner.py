"""PLANNER=fixed: preset task graphs, no LLM, free text refused. (The default setting; the other
tests run with PLANNER=gemini, see the autouse fixture in conftest.py.)"""

import json
from pathlib import Path

import pytest
from helpers import CountingMock, _acked, _wait_for
from test_planner import VALID, FakeClient

from app.budget import DailyCap
from app.config import Settings, settings
from app.dispatcher import publish_pending
from app.planner import (
    PRESET_IDS,
    PRESETS,
    GeminiPlanner,
    fixed_plan,
    make_plan,
    preset_list,
)
from app.worker import WorkerPool

TRACE_INDEX = Path(__file__).resolve().parent.parent / "web" / "public" / "traces" / "index.json"


@pytest.fixture
def fixed(monkeypatch):
    monkeypatch.setattr(settings, "planner", "fixed")


def test_the_default_is_fixed():
    assert Settings(_env_file=None).planner == "fixed"


def test_there_is_one_template_per_recorded_preset():
    recorded = [p["preset"] for p in json.loads(TRACE_INDEX.read_text())]
    assert list(PRESET_IDS) == recorded


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_every_preset_is_a_valid_dag(preset):
    plan = fixed_plan(preset)  # PlanModel validation: ids, unknown deps, cycles, 3..8 tasks
    assert 3 <= len(plan.tasks) <= 8
    assert any(t.needs_approval for t in plan.tasks), "every demo shows the approval gate"
    assert PRESETS[preset].goal


async def test_fixed_mode_never_calls_gemini():
    client = FakeClient(VALID)
    gemini = GeminiPlanner(client, "m", DailyCap(None, 5))  # a cap on None would blow up if used
    result = await make_plan("ignored free text", gemini, "fixed", "retries")
    assert result.source == "fixed"
    assert [t.id for t in result.plan.tasks] == [t.id for t in PRESETS["retries"].tasks]
    assert client.calls == []


async def test_fixed_mode_without_a_preset_uses_the_deterministic_mock_plan():
    result = await make_plan("queued before the switch", None, "fixed", None)
    assert result.source == "mock"


async def test_gemini_mode_is_unchanged_with_the_mock_client(redis):
    client = FakeClient(VALID)
    gemini = GeminiPlanner(client, "m", DailyCap(redis, 5))
    result = await make_plan("write a newsletter", gemini, "gemini")
    assert result.source == "gemini" and len(client.calls) == 1


# ---- the API ----


async def test_config_reports_the_planner_and_the_presets(client, fixed):
    body = (await client.get("/config")).json()
    assert body["planner"] == "fixed"
    assert [p["preset"] for p in body["presets"]] == list(PRESET_IDS)
    assert "key" not in json.dumps(body).lower()


async def test_config_reports_gemini_mode(client):
    assert (await client.get("/config")).json()["planner"] == "gemini"


async def test_free_text_is_refused_with_a_friendly_400_and_the_preset_list(client, pool, fixed):
    r = await client.post("/goals", json={"prompt": "plan my wedding"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "switched off" in detail
    for preset in PRESET_IDS:
        assert preset in detail
    assert await pool.fetchval("SELECT count(*) FROM goals") == 0  # nothing was created
    assert await pool.fetchval("SELECT count(*) FROM jobs") == 0


async def test_unknown_preset_is_refused(client, fixed):
    r = await client.post("/goals", json={"preset": "nope"})
    assert r.status_code == 400 and "happy-path" in r.json()["detail"]


async def test_a_preset_goal_is_planned_with_no_llm(client, pool, redis, fixed):
    r = await client.post("/goals", json={"preset": "dlq-replay-approval"})
    assert r.status_code == 201
    goal_id = r.json()["id"]
    assert await publish_pending(pool, redis) == 1
    workers = WorkerPool(pool, redis, CountingMock(), size=2, block_ms=100, planner=None)
    await workers.start()
    try:
        await _wait_for(
            lambda: pool.fetchval("SELECT status <> 'planning' FROM goals WHERE id = $1", goal_id)
        )
        await _wait_for(lambda: _acked(redis))
    finally:
        await workers.stop()
    goal = await pool.fetchrow("SELECT prompt, plan_source FROM goals WHERE id = $1", goal_id)
    assert goal["plan_source"] == "fixed"
    assert goal["prompt"] == PRESETS["dlq-replay-approval"].goal
    keys = await pool.fetchval("SELECT array_agg(key ORDER BY key) FROM tasks")
    assert keys == [t.id for t in PRESETS["dlq-replay-approval"].tasks]


async def test_gemini_mode_still_takes_free_text(client):
    assert (await client.post("/goals", json={"prompt": "anything"})).status_code == 201


async def test_gemini_mode_needs_a_prompt_or_a_preset(client):
    assert (await client.post("/goals", json={})).status_code == 400


def test_preset_list_has_ids_and_names():
    assert all(set(p) == {"preset", "name"} for p in preset_list())
