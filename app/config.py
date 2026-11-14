from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://agent:agent@localhost:5432/agent_town"
    redis_url: str = "redis://localhost:6379/0"
    gemini_api_key: str = ""
    gemini_daily_cap: int = 50


settings = Settings()
