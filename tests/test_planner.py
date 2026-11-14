"""Planner tests. Gemini is replaced by a fake client: no network, no API key, no quota."""

import json
from types import SimpleNamespace

import pytest
from google.genai import errors
from pydantic import ValidationError

from app.budget import DailyCap
from app.planner import (
    GeminiPlanner,
    MockPlanner,
    PlanModel,
    build_gemini_planner,
    fallback_plan,
    make_plan,
)
from app.providers.mock_llm import ProviderError


def task(id_: str, deps: list[str] | None = None, approval: bool = False) -> dict:
    return {
        "id": id_,
        "name": f"Task {id_}",
        "description": "does something",
        "depends_on": deps or [],
        "needs_approval": approval,
    }


def plan_json(*tasks: dict) -> str:
    return json.dumps({"tasks": list(tasks)})


VALID = plan_json(task("a"), task("b", ["a"]), task("c", ["a", "b"], approval=True))


class FakeClient:
    """Mimics client.aio.models.generate_content; replies are consumed in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(text=reply)


@pytest.fixture
def cap(redis):
    return DailyCap(redis, limit=50, today=lambda: "2026-01-01")


# --- validation (pure) -------------------------------------------------------------------------


def test_valid_plan_is_accepted():
    plan = PlanModel.model_validate_json(VALID)
    assert [t.id for t in plan.tasks] == ["a", "b", "c"]
    assert plan.tasks[2].needs_approval


@pytest.mark.parametrize("count", [0, 2, 9])
def test_task_count_must_be_3_to_8(count):
    tasks = [task(f"t{i}") for i in range(count)]
    with pytest.raises(ValidationError):
        PlanModel.model_validate({"tasks": tasks})


def test_unknown_dependency_rejected():
    with pytest.raises(ValidationError, match="unknown ids"):
        PlanModel.model_validate({"tasks": [task("a"), task("b", ["a"]), task("c", ["zzz"])]})


def test_cycle_rejected():
    tasks = [task("a"), task("b", ["a", "c"]), task("c", ["b"])]
    with pytest.raises(ValidationError, match="cycle"):
        PlanModel.model_validate({"tasks": tasks})


def test_self_dependency_rejected():
    with pytest.raises(ValidationError, match="itself"):
        PlanModel.model_validate({"tasks": [task("a"), task("b"), task("c", ["c"])]})


def test_plan_without_root_rejected():
    tasks = [task("a", ["b"]), task("b", ["c"]), task("c", ["a"])]
    with pytest.raises(ValidationError, match="no dependencies"):
        PlanModel.model_validate({"tasks": tasks})


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        PlanModel.model_validate({"tasks": [task("a"), task("a"), task("b")]})


def test_fallback_and_mock_plans_are_valid():
    PlanModel.model_validate(fallback_plan("x").model_dump())
    assert len(fallback_plan("x").tasks) == 3


async def test_mock_planner_is_deterministic():
    a = await MockPlanner().plan("write a post")
    b = await MockPlanner().plan("write a post")
    assert a.source == "mock" and a.plan == b.plan
    assert any(t.needs_approval for t in a.plan.tasks)  # exercises the approval gate


# --- Gemini planner (fake client, real Redis for the cap) --------------------------------------


async def test_gemini_valid_plan(cap):
    client = FakeClient(VALID)
    result = await GeminiPlanner(client, "m", cap).plan("goal")
    assert result.source == "gemini" and len(result.plan.tasks) == 3
    (call,) = client.calls
    # The request asks for JSON constrained by the Pydantic schema.
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].response_schema is PlanModel


async def test_invalid_json_retries_once_then_succeeds(cap):
    client = FakeClient("not json {", VALID)
    result = await GeminiPlanner(client, "m", cap).plan("goal")
    assert result.source == "gemini"
    assert len(client.calls) == 2


async def test_invalid_twice_falls_back_after_exactly_two_calls(cap):
    client = FakeClient("not json", plan_json(task("a"), task("b", ["a"]), task("c", ["c"])))
    result = await GeminiPlanner(client, "m", cap).plan("goal")
    assert result.source == "fallback"
    assert len(result.plan.tasks) == 3
    assert len(client.calls) == 2  # retry once, not forever


async def test_cycle_and_unknown_dep_from_gemini_are_treated_as_invalid(cap):
    cyc = plan_json(task("a", ["b"]), task("b", ["a"]), task("c"))
    unknown = plan_json(task("a"), task("b", ["nope"]), task("c"))
    for bad in (cyc, unknown):
        client = FakeClient(bad, VALID)
        result = await GeminiPlanner(client, "m", cap).plan("goal")
        assert result.source == "gemini" and len(client.calls) == 2  # rejected, then retried


async def test_api_error_becomes_retryable_provider_error(cap):
    from app.retry import is_retryable

    err = errors.APIError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    with pytest.raises(ProviderError) as exc:
        await GeminiPlanner(FakeClient(err), "m", cap).plan("goal")
    assert exc.value.status == 429 and is_retryable(exc.value)


# --- daily cap and key handling ----------------------------------------------------------------


async def test_cap_switches_to_mock(redis):
    cap = DailyCap(redis, limit=2, today=lambda: "2026-01-01")
    client = FakeClient(VALID, VALID, VALID)
    gemini = GeminiPlanner(client, "m", cap)
    sources = [(await make_plan("goal", gemini)).source for _ in range(3)]
    assert sources == ["gemini", "gemini", "mock"]
    assert len(client.calls) == 2  # the third call never reached Gemini


async def test_retry_counts_against_the_cap(redis):
    cap = DailyCap(redis, limit=1, today=lambda: "2026-01-01")
    client = FakeClient("bad json", VALID)
    result = await make_plan("goal", GeminiPlanner(client, "m", cap))
    assert result.source == "mock" and len(client.calls) == 1  # no budget left for the retry


async def test_cap_resets_on_a_new_day(redis):
    day = ["2026-01-01"]
    cap = DailyCap(redis, limit=1, today=lambda: day[0])
    assert await cap.acquire() and not await cap.acquire()
    day[0] = "2026-01-02"
    assert await cap.acquire()
    await redis.delete("gemini:calls:2026-01-01", "gemini:calls:2026-01-02")


async def test_concurrent_callers_never_exceed_the_cap(redis):
    import asyncio

    cap = DailyCap(redis, limit=10, today=lambda: "2026-01-03")
    results = await asyncio.gather(*(cap.acquire() for _ in range(50)))
    assert sum(results) == 10
    await redis.delete("gemini:calls:2026-01-03")


async def test_no_api_key_uses_mock(redis):
    assert build_gemini_planner(redis, "", "m", 50) is None  # no client is even created
    result = await make_plan("goal", None)
    assert result.source == "mock"
