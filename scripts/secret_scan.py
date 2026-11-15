"""Fail if a tracked file looks like it contains a secret. Runs in CI; run it before you push.

    python scripts/secret_scan.py

Deliberately small and self-contained (a few regexes over `git ls-files`), so it is easy to read
and explain. It catches the mistakes that actually happen: pasting an API key or a database URL
with a real password into a file, or committing a `.env`. It does NOT scan git history (a secret
that was committed once and deleted is still leaked: rotate it), and a purpose-built scanner such
as gitleaks is stronger; this is the cheap first line. Matches are printed masked.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# name -> regex. Each looks for the SHAPE of a real credential.
PATTERNS: dict[str, re.Pattern] = {
    "Google/Gemini API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "private key block": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "Render API key": re.compile(r"\brnd_[A-Za-z0-9]{20,}\b"),
    "Neon API key": re.compile(r"\bnapi_[a-z0-9]{30,}\b"),
    "API key assigned in code or config": re.compile(
        r"""(?i)\b(?:gemini_api_key|api[_-]?key|secret[_-]?key|auth[_-]?token|upstash\w*token)\b"""
        r"""\s*[:=]\s*["']?([A-Za-z0-9_\-]{24,})["']?"""
    ),
}
# user:password@host in a URL. Local hosts and placeholder passwords are fine.
URL_WITH_PASSWORD = re.compile(
    r"\b(?:postgres(?:ql)?|rediss?|amqps?|mongodb(?:\+srv)?)://[^:@/\s]+:([^@\s/]+)@([^/\s:?]+)"
)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "postgres", "redis", "db", "host.docker.internal"}
PLACEHOLDER = re.compile(
    r"^(?:PASSWORD|password|PASS|pass|<.*>|x+|\*+|YOUR[_A-Z]*|\$\{.*\}|\$[A-Z_]+|agent|secret|changeme)$"
)
PLACEHOLDER_HOST = re.compile(
    r"^(?:HOST|host|[A-Z_]*HOST[A-Z_]*|<.*>|\$\{.*\}|\$[A-Z_]+|.*\.example\.[a-z]+)$"
)

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".lock", ".svg"}
SKIP_NAMES = {"package-lock.json"}


def mask(value: str) -> str:
    return value[:4] + "…" + f"({len(value)} chars)"


def scan_text(name: str, text: str) -> list[str]:
    """Findings for one file's text, as 'file:line: what (masked)'."""
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for label, rx in PATTERNS.items():
            m = rx.search(line)
            if m:
                found.append(f"{name}:{n}: {label} ({mask(m.group(0))})")
                break  # one finding per line is enough
        else:
            found += _url_findings(name, n, line)
    return found


def _url_findings(name: str, n: int, line: str) -> list[str]:
    found = []
    for m in URL_WITH_PASSWORD.finditer(line):
        password, host = m.group(1), m.group(2)
        if host in LOCAL_HOSTS or PLACEHOLDER.match(password) or PLACEHOLDER_HOST.match(host):
            continue
        found.append(f"{name}:{n}: password in a URL for host {host} ({mask(password)})")
    return found


def scan_repo(root: Path = ROOT) -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    found = []
    for rel in tracked:
        path = root / rel
        base = path.name
        if base == ".env" or (base.startswith(".env.") and base != ".env.example"):
            found.append(f"{rel}: an environment file must not be committed")
            continue
        if base in SKIP_NAMES or path.suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue  # binary
        found += scan_text(rel, text)
    return found


if __name__ == "__main__":
    problems = scan_repo()
    if problems:
        print("Possible secrets in tracked files:\n  " + "\n  ".join(problems))
        print("\nRemove them, rotate any real key, and use environment variables instead.")
        sys.exit(1)
    print("secret scan: no secrets found in tracked files")
