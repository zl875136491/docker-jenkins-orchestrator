from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    worker_name: str = "local-worker"
    worker_secret: str = "local-worker-secret"
    jwt_secret: str = "local-development-jwt-secret-change-me"
    jwt_expire_minutes: int = 60
    mongodb_url: str | None = None
    celery_broker_url: str | None = None

    model_config = SettingsConfigDict(env_prefix="ORCHESTRATOR_", env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
