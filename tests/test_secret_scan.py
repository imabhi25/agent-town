"""The CI secret scan: it must flag real-looking credentials and leave placeholders alone.
Fake secrets are assembled at runtime so this file does not trip the scan itself."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("secret_scan", ROOT / "scripts" / "secret_scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

GOOGLE_KEY = "AIza" + "x" * 35
GITHUB = "ghp_" + "a" * 36


@pytest.mark.parametrize(
    "line",
    [
        f"GEMINI_API_KEY={GOOGLE_KEY}",
        f'key = "{GOOGLE_KEY}"',
        f"token: {GITHUB}",
        "-----BEGIN " + "PRIVATE KEY-----",
        "DATABASE_URL=postgresql://user:"
        + "s3cr3tpass"
        + "@ep-cool-1.neon.tech/db?sslmode=require",
        "REDIS_URL=rediss://default:" + "AbCdEf123456" + "@eu1-x.upstash.io:6379",
        "AWS_KEY=AKIA" + "A" * 16,
        "api_key = " + "k" * 30,
    ],
)
def test_flags_secrets(line):
    assert scan.scan_text("x.env", line)


@pytest.mark.parametrize(
    "line",
    [
        "DATABASE_URL=postgresql://agent:agent@localhost:5432/agent_town",
        "REDIS_URL=redis://localhost:6379/0",
        "DATABASE_URL=postgresql://USER:PASSWORD@HOST/db?sslmode=require",
        "REDIS_URL=rediss://default:<password>@<host>:6379",
        "DATABASE_URL=postgresql://user:${DB_PASSWORD}@db.internal/app",
        "GEMINI_API_KEY=",
        'gemini_api_key: str = ""',
        "# Keep the key server-side only.",
    ],
)
def test_ignores_placeholders_and_local_urls(line):
    assert scan.scan_text("x.md", line) == []


def test_findings_are_masked():
    (finding,) = scan.scan_text("f", f"GEMINI_API_KEY={GOOGLE_KEY}")
    assert GOOGLE_KEY not in finding and "f:1:" in finding


def test_the_repository_is_clean():
    assert scan.scan_repo() == []


def test_a_tracked_env_file_is_flagged(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".env").write_text("A=1\n")
    (tmp_path / ".env.example").write_text("A=\n")
    subprocess.run(["git", "add", "-A", "-f"], cwd=tmp_path, check=True)
    found = scan.scan_repo(tmp_path)
    assert len(found) == 1 and found[0].startswith(".env:")
