"""The README's chaos numbers are generated from results/chaos/campaign.jsonl, never typed."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("chaos_report", ROOT / "scripts" / "chaos_report.py")
chaos_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chaos_report)


def _row(mode: str, lost: int = 0, **faults) -> dict:
    return {
        "campaign": "c1",
        "mode": mode,
        "jobs": 100,
        "workers": 6,
        "succeeded": 100 - lost,
        "dead": 0,
        "redelivered_jobs": 5,
        "faults": {
            "worker_kills": 10,
            "worker_kills_mid_job": 7,
            "dispatcher_kills": 1,
            "redis_restarts": 1,
            **faults,
        },
        "violations": {"lost": lost},
        "total_violations": lost,
    }


def test_summary_adds_up_runs_by_mode():
    s = chaos_report.summarize([_row("safe"), _row("safe"), _row("unsafe", lost=4), _row("unsafe")])
    assert s["safe"]["jobs"] == 200 and s["safe"]["lost"] == 0
    assert s["safe"]["worker_kills"] == 20 and s["safe"]["redis_restarts"] == 2
    assert s["unsafe"]["lost"] == 4 and s["unsafe"]["runs_with_violations"] == 1


def test_headline_is_built_from_the_rows():
    block = chaos_report.render([_row("safe"), _row("unsafe", lost=3)])
    # 10 worker kills + 1 dispatcher kill + 1 Redis restart = 12 crashes
    assert "**100 jobs, 12 injected crashes, 0 lost jobs, 0 duplicate outputs**" in block
    assert "**1 of 1**" in block


def test_readme_block_matches_committed_results():
    assert chaos_report.main(["--check"]) == 0
