"""The README's benchmark numbers are generated from results/bench/engine.jsonl, never typed."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("bench_report", ROOT / "scripts" / "bench_report.py")
bench_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench_report)

ROWS = [
    {
        "kind": "meta",
        "run": "r1",
        "cpu": "Test CPU",
        "cores": 4,
        "python": "3.12.0",
        "postgres": "16.1 (Debian)",
        "mock_latency_s": [0.05, 0.2],
    },
    {"kind": "throughput", "workers": 1, "jobs": 100, "seconds": 12.5, "jobs_per_s": 8.0,
     "ideal_jobs_per_s": 8.0},
    {"kind": "throughput", "workers": 4, "jobs": 400, "seconds": 13.3, "jobs_per_s": 30.0,
     "ideal_jobs_per_s": 32.0},
    {"kind": "latency", "workers": 4, "offered_jobs_per_s": 18.0, "jobs": 270, "p50_ms": 150,
     "p95_ms": 220, "p99_ms": 240},
    {"kind": "recovery", "trial": 1, "lease_s": 15.0, "heartbeat_ttl_s": 6.0,
     "recovery_interval_s": 5.0, "kill_to_reclaim_s": 15.0, "kill_to_done_s": 17.0},
    {"kind": "recovery", "trial": 2, "lease_s": 15.0, "heartbeat_ttl_s": 6.0,
     "recovery_interval_s": 5.0, "kill_to_reclaim_s": 19.0, "kill_to_done_s": 21.0},
]


def test_table_has_one_row_per_worker_count():
    block = bench_report.render(ROWS)
    assert "| 4 | 32 | 30 | 94% | 150 / 220 / 240 ms |" in block
    assert "| 1 | 8 | 8 | 100% | n/a |" in block  # no latency row for 1 worker: says so
    assert "17.0 s median (19.0 s max)" in block  # recovery median of 15 and 19


def test_headline_uses_the_fastest_worker_count():
    line = bench_report.render_headline(ROWS)
    assert "30 jobs/s with 4 worker processes on 4 cores (mock LLM), p99 240 ms" in line


def test_readme_blocks_match_committed_results():
    assert bench_report.main(["--check"]) == 0
