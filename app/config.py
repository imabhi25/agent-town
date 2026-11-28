from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://agent:agent@localhost:5432/agent_town"
    redis_url: str = "redis://localhost:6379/0"
    # Who plans a goal. "fixed" (the public default): a built-in task graph chosen from a preset id,
    # no LLM, no API key, no quota, the same demo every time. "gemini": the real planner (validated,
    # retried once, fallback plan) when a key is set, else the mock planner.
    planner: Literal["fixed", "gemini"] = "fixed"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # Hard daily limit on real Gemini calls (counted in Redis). Over it, the mock planner is used.
    gemini_daily_cap: int = 50
    worker_count: int = 4
    # Retry backoff in seconds: delay = uniform(0, min(cap, base * 2**(attempt-1))).
    # Crash recovery: heartbeat_ttl must be shorter than lease_seconds.
    heartbeat_interval: float = 2.0
    heartbeat_ttl: float = 6.0
    lease_seconds: float = 15.0
    recovery_interval: float = 5.0
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    # Mock provider latency range in seconds (overridable per job via payload `mock_latency`).
    mock_min_latency: float = 0.05
    mock_max_latency: float = 0.2
    # How long the mock takes on a goal's task job (the "build time" of its building), reported
    # in PROGRESS_STEPS steps as job.progress events. Real Gemini calls take their own time.
    task_build_seconds: float = 4.0
    worker_block_ms: int = 500  # XREADGROUP BLOCK; the live profile raises it to save commands
    # --- protection (step 4) ---
    # Per-IP token bucket on POST /goals: burst, then refill per second.
    goal_rate_capacity: float = 5
    goal_rate_per_sec: float = 0.1
    # Browser origins allowed to call the API (comma separated). Step 9 sets it to the Vercel URL.
    cors_origins: str = "http://localhost:5173"
    trust_forwarded_for: bool = False  # True behind a proxy that sets X-Forwarded-For (Render)
    live_goals_daily_cap: int = 30  # goals per UTC day across everyone; 0 = unlimited
    # Downstream token bucket for provider calls (per provider name).
    provider_rate_per_sec: float = 100
    provider_burst: float = 100
    # Circuit breaker: open after N consecutive failures, probe again after open_seconds.
    circuit_failure_threshold: int = 5
    circuit_open_seconds: float = 10
    circuit_probe_timeout: float = 10  # a half-open probe that never reports back expires
    # Redis command budget (Upstash free: 500K/month). Past the guard ratio, live mode is off.
    redis_monthly_limit: int = 500_000
    redis_budget_guard_ratio: float = 0.9
    # --- chaos and load (step 5) ---
    flood_max: int = 2000  # most mock jobs one flood request may create (100 in the live demo)
    chaos_rate_capacity: float = 20  # per-IP token bucket on chaos endpoints
    chaos_rate_per_sec: float = 1.0
    load_generator: bool = False  # keeps a local system lightly busy; always OFF in the live demo
    load_interval: float = 2.0  # seconds between load generator ticks
    # --- deployment (step 9) ---
    # Apply migrations/*.sql at startup (a managed database starts empty). Off locally, where
    # docker compose loads them.
    auto_migrate: bool = False
    # Neon's "-pooler" host is PgBouncer in transaction mode, which cannot keep prepared
    # statements. Auto-detected from the host name; set true to force it for another pooler.
    db_pooler: bool = False
    # --- live demo profile (free-tier hosting) ---
    live_demo: bool = False
    embedded_workers: bool = False  # run dispatcher + workers inside the API process
    idle_sleep_seconds: float = 300  # live demo: sleep after this long with no new goals
    # Hard limit on one provider call. A hung call becomes a retryable TimeoutError.
    provider_timeout: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _heartbeat_shorter_than_lease(self) -> "Settings":
        # If the heartbeat outlived the lease, a dead worker would still look alive when its
        # message becomes claimable, and recovery would skip it. Fail at startup, not in a crash.
        if self.heartbeat_ttl >= self.lease_seconds:
            raise ValueError(
                f"heartbeat_ttl ({self.heartbeat_ttl}) must be less than "
                f"lease_seconds ({self.lease_seconds})"
            )
        if self.live_demo:
            # "Live demo" profile: few, quiet workers so a free Redis (500K commands/month) and a
            # free Postgres (compute hours) last. Overrides explicit values on purpose: one switch.
            self.embedded_workers = True
            self.flood_max = min(self.flood_max, 100)
            self.load_generator = False  # nobody is watching; it would keep the system awake
            self.worker_count = 2
            self.heartbeat_interval = 10.0
            self.heartbeat_ttl = 30.0
            self.lease_seconds = 45.0
            self.recovery_interval = 30.0
            self.worker_block_ms = 5000
            if self.heartbeat_ttl >= self.lease_seconds:  # pragma: no cover (constants above)
                raise ValueError("live profile: heartbeat_ttl must be less than lease_seconds")
        return self


settings = Settings()


LOCAL_ORIGIN_HINTS = ("localhost", "127.0.0.1", "[::1]")


def production_problems(s: Settings) -> list[str]:
    """Things that must be right before the public demo starts. Empty = fine. Checked at startup
    when LIVE_DEMO is on, so a bad deploy fails loudly instead of running half-open."""
    problems = []
    origins = [o.strip() for o in s.cors_origins.split(",") if o.strip()]
    if not origins:
        problems.append("CORS_ORIGINS is empty: set it to the Vercel site's origin.")
    for o in origins:
        if o == "*":
            problems.append("CORS_ORIGINS must not be '*': list the Vercel origin only.")
        elif not o.startswith("https://"):
            problems.append(
                f"CORS_ORIGINS entry {o!r} must be an https:// origin in the live demo."
            )
        elif any(h in o for h in LOCAL_ORIGIN_HINTS):
            problems.append(f"CORS_ORIGINS entry {o!r} points at localhost.")
        elif o.endswith("/"):
            problems.append(f"CORS_ORIGINS entry {o!r} must have no trailing slash.")
    if not s.trust_forwarded_for:
        problems.append(
            "TRUST_FORWARDED_FOR must be true behind Render's proxy, or every visitor shares one "
            "per-IP rate limit."
        )
    return problems
