"""The 3D town layout (docs/reference/town-layout.json) against the real plans: every preset's
task graph must fit the zones and slots, and the committed JSON must be what the seeded generator
writes. (Geometry tests, no overlaps and reachability, are in web/src/city3d/town.test.ts.)"""

import json
import runpy
from pathlib import Path

import pytest

from app.planner import PRESET_IDS, fixed_plan

ROOT = Path(__file__).resolve().parent.parent
LAYOUT = json.loads((ROOT / "docs" / "reference" / "town-layout.json").read_text())
TRACES = ROOT / "web" / "public" / "traces"


def stage_sizes(deps: dict[str, list[str]]) -> list[int]:
    """Tasks per DAG stage: a task's stage is 1 + the deepest stage among its dependencies."""
    stage: dict[str, int] = {}

    def of(t: str) -> int:
        if t not in stage:
            stage[t] = 1 + max((of(d) for d in deps[t]), default=0)
        return stage[t]

    for t in deps:
        of(t)
    return [sum(1 for s in stage.values() if s == n) for n in range(1, max(stage.values()) + 1)]


def fits(sizes: list[int]) -> None:
    zones = LAYOUT["zones"]
    assert 1 <= len(sizes) <= 8
    # stages beyond the last zone merge into it
    merged = sizes[: len(zones)] + [sum(sizes[len(zones) :])] if len(sizes) > len(zones) else sizes
    merged = [m for m in merged if m]
    for zone, n in zip(zones, merged, strict=False):
        assert n <= len(zone["slots"]), f"{zone['id']} would need {n} slots"


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_every_fixed_preset_fits_the_town(preset):
    deps = {t.id: t.depends_on for t in fixed_plan(preset).tasks}
    sizes = stage_sizes(deps)
    assert len(sizes) <= 6, "a preset should not need merged zones"
    fits(sizes)


@pytest.mark.parametrize(
    "trace", sorted(p.stem for p in TRACES.glob("*.json") if p.stem != "index")
)
def test_every_recorded_trace_fits_the_town(trace):
    events = json.loads((TRACES / f"{trace}.json").read_text())["events"]
    created = [e["payload"] for e in events if e["type"] == "task.created"]
    ids = {p["key"]: p["task_id"] for p in created}
    by_id = {p["task_id"]: p for p in created}
    deps = {p["task_id"]: list(p["depends_on"]) for p in by_id.values()}
    assert deps and all(d in deps for ds in deps.values() for d in ds), ids
    fits(stage_sizes(deps))


def test_worst_case_plan_fits():
    # 8 tasks in a chain (8 stages: the last three merge into zone 6), and 8 in one stage
    chain = {f"t{i}": ([f"t{i - 1}"] if i > 1 else []) for i in range(1, 9)}
    fits(stage_sizes(chain))
    wide = {"t1": [], **{f"t{i}": ["t1"] for i in range(2, 9)}}
    fits(stage_sizes(wide))


def test_the_committed_layout_is_what_the_seeded_generator_writes(tmp_path, monkeypatch):
    committed = (ROOT / "docs" / "reference" / "town-layout.json").read_text()
    runpy.run_path(str(ROOT / "scripts" / "gen_town_layout.py"))
    assert (ROOT / "docs" / "reference" / "town-layout.json").read_text() == committed
