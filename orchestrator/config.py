from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: Literal["development", "test", "production"] = "development"
    worker_name: str = "local-worker"
    worker_region: str = Field(
        default="default",
        validation_alias=AliasChoices("WORKER_REGION", "ORCHESTRATOR_WORKER_REGION", "worker_region"),
    )
    worker_secret: str = "local-worker-secret"
    jwt_secret: str = "local-development-jwt-secret-change-me"
    jwt_expire_minutes: int = 60
    jwt_refresh_expire_days: int = 30

    storage_backend: Literal["memory", "mongo"] = "memory"
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_database: str = "orchestrator"
    mongodb_timeout_ms: int = 5000
    data_encryption_key: str | None = None

    task_dispatcher: Literal["memory", "celery"] = "memory"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_build_queue: str = "orchestrator.builds"
    celery_images_queue: str = "orchestrator.images"
    celery_poll_interval_seconds: int = 15
    celery_max_poll_attempts: int = 240
    celery_recovery_interval_seconds: int = 300
    base_image_sync_interval_hours: int = 24

    gitlab_url: str | None = None
    gitlab_token: str | None = None
    jenkins_url: str | None = None
    jenkins_user: str | None = None
    jenkins_password: str | None = None
    jenkins_job_name: str = "apps-orchestrator"
    harbor_url: str | None = None
    harbor_user: str | None = None
    harbor_password: str | None = None
    harbor_project: str = "apps"
    harbor_boot_images_project: str = "boot-images"
    docker_base_url: str | None = None
    docker_services_network: str = "orchestrator"
    deployment_readiness_timeout_seconds: int = 60
    deployment_readiness_poll_interval_seconds: float = 1.0
    deployment_port_range_start: int = 18000
    deployment_port_range_end: int = 18999
    external_request_timeout_seconds: int = 30
    public_host: str | None = None
    public_scheme: Literal["http", "https"] = "http"

    model_config = SettingsConfigDict(env_prefix="ORCHESTRATOR_", env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_production_dependencies(self) -> "Settings":
        if not 1 <= self.deployment_port_range_start <= 65535:
            raise ValueError("ORCHESTRATOR_DEPLOYMENT_PORT_RANGE_START must be between 1 and 65535")
        if not 1 <= self.deployment_port_range_end <= 65535:
            raise ValueError("ORCHESTRATOR_DEPLOYMENT_PORT_RANGE_END must be between 1 and 65535")
        if self.deployment_port_range_start > self.deployment_port_range_end:
            raise ValueError("ORCHESTRATOR_DEPLOYMENT_PORT_RANGE_START must not exceed END")
        if self.storage_backend == "mongo" and not self.data_encryption_key:
            raise ValueError("ORCHESTRATOR_DATA_ENCRYPTION_KEY is required when storage_backend=mongo")
        if self.environment == "production":
            if self.storage_backend != "mongo" or self.task_dispatcher != "celery":
                raise ValueError("production requires MongoDB storage and Celery dispatch")
            if not self.jenkins_url or not self.harbor_url or not self.docker_base_url:
                raise ValueError("production requires Jenkins, Harbor, and Docker endpoint configuration")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
