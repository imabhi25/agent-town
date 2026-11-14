from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://agent:agent@localhost:5432/agent_town"
    redis_url: str = "redis://localhost:6379/0"
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
        return self


settings = Settings()
