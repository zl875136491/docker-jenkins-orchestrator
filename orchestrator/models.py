from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BuildStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class UserAppCreate(BaseModel):
    appid: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    name: str = Field(min_length=1, max_length=128)
    repository_url: HttpUrl
    environment: dict[str, str] = Field(default_factory=dict)
    components: list[str] = Field(default_factory=list)


class UserApp(UserAppCreate):
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BuildCreate(BaseModel):
    git_ref: str = Field(default="main", min_length=1, max_length=256)


class BuildJob(BaseModel):
    build_id: str
    appid: str
    git_ref: str
    status: BuildStatus = BuildStatus.QUEUED
    jenkins_job_id: str | None = None
    images: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BaseImage(BaseModel):
    image: str = Field(min_length=1)
    harbor_repository: str
    status: str = "pending"
    synced_at: datetime | None = None
