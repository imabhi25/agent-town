"""Goal planner: turns a prompt into a validated task DAG.

Two implementations behind one interface: GeminiPlanner (real, capped) and MockPlanner
(deterministic; used in tests, CI, with no API key, and when the daily cap is hit).
Gemini output is never trusted: it is parsed with Pydantic, checked for unknown dependencies and
cycles, retried once if invalid, and replaced by a generic fallback plan if it is still invalid.
"""

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.budget import DailyCap
from app.providers.mock_llm import ProviderError

log = logging.getLogger("planner")

PLAN_ATTEMPTS = 2  # first try plus one retry


class PlanTask(BaseModel):
    # No defaults on purpose: the Gemini response_schema marks every field required.
    id: str
    name: str
    description: str
    depends_on: list[str]
    needs_approval: bool


class PlanModel(BaseModel):
    tasks: list[PlanTask] = Field(min_length=3, max_length=8)  # also tells Gemini the bounds

    @model_validator(mode="after")
    def _valid_dag(self) -> "PlanModel":
        tasks = self.tasks
        ids = [t.id for t in tasks]
        if any(not t.id.strip() or not t.name.strip() for t in tasks):
            raise ValueError("task id and name must not be empty")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate task ids")
        known = set(ids)
        for t in tasks:
            unknown = [d for d in t.depends_on if d not in known]
            if unknown:
                raise ValueError(f"task {t.id} depends on unknown ids {unknown}")
            if t.id in t.depends_on:
                raise ValueError(f"task {t.id} depends on itself")
        if not any(not t.depends_on for t in tasks):
            raise ValueError("at least one task must have no dependencies")
        _reject_cycles(tasks)
        return self


def _reject_cycles(tasks: list[PlanTask]) -> None:
    """Kahn's algorithm: repeatedly remove tasks whose dependencies are all removed.
    Anything left over sits on, or behind, a cycle."""
    remaining = {t.id: set(t.depends_on) for t in tasks}
    while True:
        ready = [i for i, deps in remaining.items() if not deps]
        if not ready:
            break
        for i in ready:
            del remaining[i]
        for deps in remaining.values():
            deps.difference_update(ready)
    if remaining:
        raise ValueError(f"dependency cycle among tasks {sorted(remaining)}")


@dataclass
class PlanResult:
    plan: PlanModel
    source: str  # "gemini" | "mock" | "fallback"


class CapExceeded(Exception):
    """The daily Gemini cap is used up; the caller should use the mock planner."""


class Planner(Protocol):
    async def plan(self, prompt: str) -> PlanResult: ...


def fallback_plan(prompt: str) -> PlanModel:
    """Generic 3-step plan used when Gemini output stays invalid."""
    return PlanModel(
        tasks=[
            PlanTask(
                id="t1",
                name="Research",
                description=f"Gather the key facts needed for: {prompt[:200]}",
                depends_on=[],
                needs_approval=False,
            ),
            PlanTask(
                id="t2",
                name="Draft",
                description="Write a first version using the research.",
                depends_on=["t1"],
                needs_approval=False,
            ),
            PlanTask(
                id="t3",
                name="Review",
                description="Check the draft and produce the final result.",
                depends_on=["t2"],
                needs_approval=False,
            ),
        ]
    )


class MockPlanner:
    """Deterministic diamond-shaped plan with an approval gate at the end, so every feature of
    the pipeline (parallel tasks, ordering, approval) is exercised without any API."""

    async def plan(self, prompt: str) -> PlanResult:
        topic = prompt[:80]
        plan = PlanModel(
            tasks=[
                PlanTask(
                    id="t1",
                    name="Research",
                    description=f"Research: {topic}",
                    depends_on=[],
                    needs_approval=False,
                ),
                PlanTask(
                    id="t2",
                    name="Outline",
                    description=f"Outline the approach for: {topic}",
                    depends_on=["t1"],
                    needs_approval=False,
                ),
                PlanTask(
                    id="t3",
                    name="Draft",
                    description=f"Write a draft for: {topic}",
                    depends_on=["t1"],
                    needs_approval=False,
                ),
                PlanTask(
                    id="t4",
                    name="Review and publish",
                    description="Combine outline and draft into the final result.",
                    depends_on=["t2", "t3"],
                    needs_approval=True,
                ),
            ]
        )
        return PlanResult(plan, "mock")


SYSTEM_PROMPT = (
    "You are a project planner. Break the user's goal into 3 to 8 tasks that form a dependency "
    "graph. Give each task a short unique id (t1, t2, ...), a name, a one-sentence description, "
    "the ids of tasks it depends on (it may only depend on other tasks in the plan; at least one "
    "task must have no dependencies; no cycles) and needs_approval=true only for a step a human "
    "should confirm before it runs, such as publishing or sending something."
)


class GeminiPlanner:
    """Real planner. `client` is a google-genai Client (or a test double with the same shape)."""

    def __init__(self, client, model: str, cap: DailyCap):
        self.client, self.model, self.cap = client, model, cap

    async def plan(self, prompt: str) -> PlanResult:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=PlanModel,
        )
        for attempt in range(1, PLAN_ATTEMPTS + 1):
            if not await self.cap.acquire():
                raise CapExceeded()
            try:
                response = await self.client.aio.models.generate_content(
                    model=self.model, contents=f"Goal: {prompt}", config=config
                )
            except errors.APIError as e:
                # Surface as a ProviderError so the job's normal retry rules apply (429/5xx retry).
                raise ProviderError(int(e.code or 500), f"gemini: {e.message}") from e
            try:
                return PlanResult(PlanModel.model_validate_json(response.text or ""), "gemini")
            except (ValidationError, ValueError, json.JSONDecodeError) as e:
                log.warning("planner attempt %d returned an invalid plan: %s", attempt, e)
        return PlanResult(fallback_plan(prompt), "fallback")


def build_gemini_planner(redis, api_key: str, model: str, daily_cap: int) -> GeminiPlanner | None:
    """None when there is no API key: the system then runs on the mock planner only."""
    if not api_key:
        return None
    from google import genai

    return GeminiPlanner(genai.Client(api_key=api_key), model, DailyCap(redis, daily_cap))


# ---- fixed planner (PLANNER=fixed) -------------------------------------------------------------
# One template per recorded preset (the same ids as web/public/traces/). Nothing here calls an LLM
# and the plan does not depend on any free text, so the public demo is the same every time.


def _t(id: str, name: str, description: str, deps: list[str], approval: bool = False) -> PlanTask:
    return PlanTask(
        id=id, name=name, description=description, depends_on=deps, needs_approval=approval
    )


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    goal: str  # the goal text stored for this preset
    tasks: tuple[PlanTask, ...]


PRESETS: dict[str, Preset] = {
    p.id: p
    for p in (
        Preset(
            "happy-path",
            "Happy path",
            "Plan a trip to Montreal",
            (
                _t("t1", "Research", "Research: Plan a trip to Montreal", []),
                _t("t2", "Outline", "Outline the approach for: Plan a trip to Montreal", ["t1"]),
                _t("t3", "Draft", "Write a draft for: Plan a trip to Montreal", ["t1"]),
                _t(
                    "t4",
                    "Review and publish",
                    "Combine outline and draft into the final result.",
                    ["t2", "t3"],
                    True,
                ),
            ),
        ),
        Preset(
            "retries",
            "Retries with backoff",
            "Compare 3 laptops",
            (
                _t("t1", "Gather options", "List the three laptops to compare.", []),
                _t("t2", "Compare prices", "Collect current prices for each laptop.", ["t1"]),
                _t("t3", "Compare features", "Collect specs and reviews for each laptop.", ["t1"]),
                _t("t4", "Score", "Score each laptop on price and features.", ["t2", "t3"]),
                _t("t5", "Recommend", "Write the recommendation.", ["t4"], True),
            ),
        ),
        Preset(
            "chaos-recovery",
            "Chaos recovery",
            "Write a launch checklist",
            (
                _t("t1", "Collect requirements", "List what the launch needs.", []),
                _t("t2", "Draft checklist", "Write the checklist items.", ["t1"]),
                _t("t3", "Review risks", "List what could go wrong.", ["t1"]),
                _t("t4", "Assign owners", "Give each item an owner.", ["t1"]),
                _t("t5", "Publish", "Publish the final checklist.", ["t2", "t3", "t4"], True),
            ),
        ),
        Preset(
            "dlq-replay-approval",
            "DLQ, approval and replay",
            "Draft and publish a newsletter",
            (
                _t("t1", "Collect news", "Gather this month's news items.", []),
                _t("t2", "Write", "Write the newsletter text.", ["t1"]),
                _t("t3", "Proofread", "Fix mistakes and tighten the text.", ["t2"]),
                _t("t4", "Publish", "Send the newsletter.", ["t3"], True),
            ),
        ),
    )
}
PRESET_IDS = tuple(PRESETS)


def preset_list() -> list[dict[str, str]]:
    return [{"preset": p.id, "name": p.name} for p in PRESETS.values()]


def fixed_plan(preset: str) -> PlanModel:
    """The task graph for a preset id. Raises KeyError for an unknown id."""
    return PlanModel(tasks=list(PRESETS[preset].tasks))


async def make_plan(
    prompt: str, gemini: Planner | None, mode: str = "gemini", preset: str | None = None
) -> PlanResult:
    """Pick the planner. "fixed": a preset template, never an LLM (a job without a valid preset,
    for example one queued before the switch, gets the deterministic mock plan). "gemini": Gemini
    when configured and under the daily cap, otherwise the mock."""
    if mode == "fixed":
        if preset in PRESETS:
            return PlanResult(fixed_plan(preset), "fixed")
        return await MockPlanner().plan(prompt)
    if gemini is not None:
        try:
            return await gemini.plan(prompt)
        except CapExceeded:
            log.warning("daily Gemini cap reached; using the mock planner")
    return await MockPlanner().plan(prompt)
