"""The committed preset traces (web/public/traces/) must match the event schema and must show
what each preset promises. They are recorded from the real engine by scripts/record_trace.py."""

import json
from pathlib import Path

import pytest

from app.trace import Trace, check_trace

TRACES = Path(__file__).resolve().parent.parent / "web" / "public" / "traces"
FILES = sorted(p for p in TRACES.glob("*.json") if p.name != "index.json")


def test_four_presets_are_committed_and_indexed():
    index = json.loads((TRACES / "index.json").read_text())
    assert [i["preset"] for i in index] == [
        "happy-path",
        "retries",
        "chaos-recovery",
        "dlq-replay-approval",
    ]
    assert {p.stem for p in FILES} == {i["preset"] for i in index}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_trace_validates_and_shows_its_preset(path):
    trace = Trace.model_validate_json(path.read_text())
    assert trace.preset == path.stem
    assert check_trace(trace) == []


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_events_have_the_wire_shape(path):
    """Same keys as a /ws event, so the frontend feeds them to the live store unchanged."""
    for e in json.loads(path.read_text())["events"]:
        assert set(e) == {"id", "stream_id", "type", "payload", "ts"}


def test_check_trace_rejects_a_trace_that_does_not_show_its_preset():
    base = json.loads((TRACES / "happy-path.json").read_text())
    base["preset"] = "retries"  # a clean run is not a retries demo
    problems = check_trace(Trace.model_validate(base))
    assert any("retries" in p for p in problems)


def test_schema_rejects_unsorted_or_duplicate_events():
    base = json.loads((TRACES / "happy-path.json").read_text())
    dup = {**base, "events": [base["events"][0], base["events"][0]]}
    with pytest.raises(ValueError, match="duplicate"):
        Trace.model_validate(dup)
    unsorted_ = {**base, "events": list(reversed(base["events"]))}
    with pytest.raises(ValueError, match="sorted"):
        Trace.model_validate(unsorted_)
