from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class BuildStatus(str, Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    TRIGGERING = "triggering"
    BUILDING = "building"
    DEPLOYING = "deploying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_BUILD_STATUSES = {BuildStatus.SUCCEEDED, BuildStatus.FAILED, BuildStatus.CANCELLED}
ACTIVE_BUILD_STATUSES = set(BuildStatus) - TERMINAL_BUILD_STATUSES


class EventLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class UserAppCreate(DomainModel):
    appid: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    name: str = Field(min_length=1, max_length=128)
    repository_url: str = Field(min_length=3, max_length=2048)
    git_ref: str = Field(default="main", min_length=1, max_length=256)
    environment: dict[str, str] = Field(default_factory=dict)
    compose: dict[str, Any] | None = None
    components: list[str] = Field(default_factory=list)

    @field_validator("repository_url")
    @classmethod
    def repository_url_is_supported(cls, value: str) -> str:
        supported_prefixes = ("https://", "http://", "ssh://", "git@")
        if not value.startswith(supported_prefixes):
            raise ValueError("repository_url must be an HTTP(S), SSH, or git@ URL")
        return value

    @field_validator("environment")
    @classmethod
    def environment_is_valid(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not key or not (key[0].isalpha() or key[0] == "_") or not key.replace("_", "").isalnum():
                raise ValueError(f"invalid environment variable name: {key}")
            if not isinstance(item, str):
                raise ValueError(f"environment value for {key} must be a string")
        return value


class UserAppUpdate(DomainModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    repository_url: str | None = Field(default=None, min_length=3, max_length=2048)
    git_ref: str | None = Field(default=None, min_length=1, max_length=256)
    environment: dict[str, str] | None = None
    compose: dict[str, Any] | None = None
    components: list[str] | None = None

    @field_validator("repository_url")
    @classmethod
    def updated_repository_url_is_supported(cls, value: str | None) -> str | None:
        if value is not None:
            return UserAppCreate.repository_url_is_supported(value)
        return value

    @field_validator("environment")
    @classmethod
    def updated_environment_is_valid(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None:
            return UserAppCreate.environment_is_valid(value)
        return value


class UserAppRecord(DomainModel):
    appid: str
    name: str
    repository_url: str
    git_ref: str
    environment_ciphertext: str
    environment_keys: list[str] = Field(default_factory=list)
    compose: dict[str, Any] | None = None
    components: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UserApp(DomainModel):
    appid: str
    name: str
    repository_url: str
    git_ref: str
    environment_keys: list[str] = Field(default_factory=list)
    compose: dict[str, Any] | None = None
    components: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: UserAppRecord) -> "UserApp":
        return cls(**record.model_dump(exclude={"environment_ciphertext"}))


class BuildCreate(DomainModel):
    git_ref: str | None = Field(default=None, min_length=1, max_length=256)


class BuildJob(DomainModel):
    build_id: str
    appid: str
    git_ref: str
    git_commit_sha: str | None = None
    status: BuildStatus = BuildStatus.QUEUED
    celery_task_id: str | None = None
    jenkins_queue_url: str | None = None
    jenkins_build_number: int | None = None
    images: list[str] = Field(default_factory=list)
    service_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_polled_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BuildHistoryPage(DomainModel):
    """Paginated build history returned by the control plane."""

    items: list[BuildJob] = Field(default_factory=list)
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class AppEvent(DomainModel):
    event_id: str
    appid: str
    build_id: str | None = None
    kind: str
    level: EventLevel = EventLevel.INFO
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class UserImage(DomainModel):
    image_id: str
    appid: str
    build_id: str
    reference: str
    digest: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class BaseImage(DomainModel):
    source_image: str = Field(validation_alias=AliasChoices("source_image", "image"))
    harbor_reference: str = Field(validation_alias=AliasChoices("harbor_reference", "harbor_repository"))
    digest: str | None = None
    status: str = "pending"
    error: str | None = None
    synced_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def image(self) -> str:
        return self.source_image

    @property
    def harbor_repository(self) -> str:
        return self.harbor_reference


class DeploymentService(DomainModel):
    service_id: str
    appid: str
    build_id: str
    service_name: str
    image: str
    status: str
    endpoint: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Alert(DomainModel):
    alert_id: str
    appid: str
    build_id: str | None = None
    severity: str
    message: str
    resolved_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
