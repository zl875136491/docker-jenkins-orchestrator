from functools import lru_cache
from pathlib import Path
from typing import Literal, Annotated
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
import json


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
    codex_executable: str = Field(default="codex", min_length=1)
    codex_provider_name: str | None = Field(default=None, validation_alias=AliasChoices("CODEX_PROVIDER_NAME", "codex_provider_name"), repr=False)
    codex_provider_endpoint: str | None = Field(default=None, validation_alias=AliasChoices("CODEX_PROVIDER_ENDPOINT", "codex_provider_endpoint"), repr=False)
    codex_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("CODEX_API_KEY", "codex_api_key"), repr=False)
    codex_https_proxy: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ORCHESTRATOR_CODEX_HTTPS_PROXY", "codex_https_proxy"), repr=False,
    )
    codex_default_model: str | None = Field(default=None, validation_alias=AliasChoices("CODEX_DEFAULT_MODEL", "codex_default_model"), repr=False)
    codex_default_effort: str | None = Field(default=None, validation_alias=AliasChoices("CODEX_DEFAULT_EFFORT", "codex_default_effort"), repr=False)
    codex_provider_id: str = Field(default="department-relay", validation_alias=AliasChoices("CODEX_PROVIDER_ID", "codex_provider_id"))
    codex_provider_wire_api: str = Field(default="responses", validation_alias=AliasChoices("CODEX_PROVIDER_WIRE_API", "codex_provider_wire_api"))
    codex_provider_requires_openai_auth: bool = Field(
        default=False, validation_alias=AliasChoices("CODEX_PROVIDER_REQUIRES_OPENAI_AUTH", "codex_provider_requires_openai_auth"),
    )
    route_check_lease_seconds: int = Field(default=120, ge=10)
    route_probe_timeout_seconds: float = Field(default=15, gt=0, le=120)
    route_probe_max_response_bytes: int = Field(default=65536, ge=1024, le=1048576)

    gitlab_url: str | None = None
    gitlab_token: str | None = None
    jenkins_url: str | None = None
    jenkins_user: str | None = None
    jenkins_password: str | None = None
    jenkins_job_name: str = "apps-orchestrator"
    harbor_url: str | None = None
    harbor_user: str | None = None
    harbor_password: str | None = None
    harbor_ca_file: Path | None = None
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
    git_clone_timeout_seconds: int = Field(default=600, gt=0)
    workspace_root: Path = Path("var/workspaces")
    workspace_host_root: Path | None = None
    workspace_download_base_url: str | None = None
    workspace_max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    workspace_max_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    workspace_max_files: int = Field(default=10000, gt=0)
    workspace_shared_storage: bool = False
    cleanup_timeout_seconds: int = Field(default=120, gt=0)
    cleanup_docker_targets: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)

    @field_validator("harbor_ca_file", mode="before")
    @classmethod
    def normalize_harbor_ca_file(cls, value):
        return None if value == "" else value

    @field_validator(
        "codex_provider_name", "codex_provider_endpoint", "codex_default_model", "codex_default_effort",
        mode="before",
    )
    @classmethod
    def normalize_optional_codex_strings(cls, value):
        return None if value == "" else value

    @field_validator("codex_api_key", mode="before")
    @classmethod
    def normalize_optional_codex_api_key(cls, value):
        return None if value == "" else value

    @field_validator("codex_https_proxy", mode="before")
    @classmethod
    def normalize_optional_codex_https_proxy(cls, value):
        return None if value == "" else value

    @field_validator("cleanup_docker_targets", mode="before")
    @classmethod
    def parse_cleanup_targets(cls, value):
        return json.loads(value) if isinstance(value, str) and value.strip() else {} if value is None or value == "" else value

    model_config = SettingsConfigDict(env_prefix="ORCHESTRATOR_", env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_production_dependencies(self) -> "Settings":
        if self.workspace_host_root is not None and not self.workspace_host_root.is_absolute():
            raise ValueError("ORCHESTRATOR_WORKSPACE_HOST_ROOT must be absolute")
        for target, endpoint in self.cleanup_docker_targets.items():
            parsed = urlsplit(endpoint)
            valid = target.startswith(("docker:", "docker-node:")) and not any(character.isspace() for character in target)
            valid = valid and parsed.scheme in {"unix", "tcp", "http", "https", "ssh"} and not parsed.password and not parsed.query and not parsed.fragment
            if not valid or not (parsed.path.startswith("/") if parsed.scheme == "unix" else parsed.hostname):
                raise ValueError("ORCHESTRATOR_CLEANUP_DOCKER_TARGETS must map execution identities to controlled Docker endpoints")
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
