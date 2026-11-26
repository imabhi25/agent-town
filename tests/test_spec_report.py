"""The README's model-checking block is generated from results/spec/tlc.jsonl (no Java needed)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_spec", ROOT / "scripts" / "check_spec.py")
check_spec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_spec)


def test_readme_block_matches_results():
    rows = check_spec.load()
    assert (ROOT / "README.md").read_text() == check_spec.readme_with(check_spec.report(rows))


def test_every_variant_gave_the_expected_result():
    rows = {r["variant"]: r for r in check_spec.load()}
    assert set(rows) == set(check_spec.VARIANTS)
    assert all(r["as_expected"] for r in rows.values())
    assert all(rows[n]["outcome"] == "ok" for n in rows if n.startswith("correct"))


def test_parse_reads_tlc_output():
    ok = check_spec.parse(
        "Model checking completed. No error has been found.\n"
        "1,234 states generated, 567 distinct states found, 0 states left on queue.\n"
        "The depth of the complete state graph search is 21.\n"
    )
    assert ok == {"outcome": "ok", "states": 1234, "distinct": 567, "depth": 21,
                  "trace_length": None}  # fmt: skip
    bad = check_spec.parse(
        "Error: Invariant NoLostJob is violated.\nState 1: <Init>\nState 2: <X>\n"
    )
    assert bad["outcome"] == "NoLostJob" and bad["trace_length"] == 2
    for line in (
        "Temporal properties were violated.",
        "Temporal properties A and B were violated.",
    ):
        assert check_spec.parse(f"Error: {line}\n")["outcome"] == "liveness"
