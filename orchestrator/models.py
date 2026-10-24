from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4
import re

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from pydantic import SerializerFunctionWrapHandler, field_serializer, model_validator

from orchestrator.naming import APPID_MAX_LENGTH, NamingError, validate_appid


UTC_PLUS_EIGHT = timezone(timedelta(hours=8))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @field_serializer("*", mode="wrap", when_used="json")
    def serialize_json_field(self, value: Any, handler: SerializerFunctionWrapHandler) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.astimezone(UTC_PLUS_EIGHT)
        return handler(value)


def _json_paths(value: Any, path: str = "$") -> list[str]:
    """Return every object/array path used by the line-comment contract."""

    paths = [path]
    if isinstance(value, dict):
        for key, child in value.items():
            paths.extend(_json_paths(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_json_paths(child, f"{path}[{index}]"))
    return paths


def json_structure_paths(value: Any) -> list[str]:
    """Expose deterministic JSONPath-like paths for API and service validation."""

    return _json_paths(value)


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


class CleanupStatus(str, Enum):
    NOT_STARTED = "not_started"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILED = "partial_failed"


class ResourceState(str, Enum):
    PLANNED = "planned"
    PRESENT = "present"
    REMOVED = "removed"
    BLOCKED = "blocked"
    ERROR = "error"


class BuildResource(DomainModel):
    key: str
    kind: Literal["service", "container", "network", "volume", "workspace", "runtime_directory", "local_image", "registry_image", "port", "builder_cache"]
    name: str
    target: str = "docker"
    native_id: str | None = None
    managed: bool = True
    state: ResourceState = ResourceState.PLANNED
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BuildResources(DomainModel):
    build_id: str
    appid: str
    ownership_version: int = 1
    namespace: str | None = None
    repository_url: str | None = Field(default=None, repr=False)
    environment_ciphertext: str | None = Field(default=None, repr=False)
    jenkins_trigger_pending: bool = False
    resources: dict[str, BuildResource] = Field(default_factory=dict)
    cleanup_status: CleanupStatus = CleanupStatus.NOT_STARTED
    cleanup_generation: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    cleanup_requested_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    registry_storage_status: str = "not_applicable"
    updated_at: datetime = Field(default_factory=utc_now)


class EventLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class UserAppCreate(DomainModel):
    appid: str = Field(min_length=1, max_length=APPID_MAX_LENGTH)
    name: str = Field(min_length=1, max_length=128)
    repository_url: str = Field(default="", max_length=2048)
    git_ref: str = Field(default="main", min_length=1, max_length=256)
    environment: dict[str, str] = Field(default_factory=dict)
    compose: dict[str, Any] | None = None
    components: list[str] = Field(default_factory=list)
    require_route: str | None = Field(default=None, max_length=2048)

    @field_validator("require_route")
    @classmethod
    def route_is_a_single_path(cls, value: str | None) -> str | None:
        if value is not None and (not value.startswith("/") or value.startswith("//") or
                                  any(char in value for char in "?#\\") or
                                  any(ord(char) <= 32 or ord(char) == 127 for char in value)):
            raise ValueError("require_route must be a single absolute route path without query or fragment")
        return value

    @field_validator("appid")
    @classmethod
    def appid_is_valid(cls, value: str) -> str:
        try:
            return validate_appid(value)
        except NamingError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("repository_url")
    @classmethod
    def repository_url_is_supported(cls, value: str) -> str:
        if not value:
            return value
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
    repository_url: str | None = Field(default=None, max_length=2048)
    git_ref: str | None = Field(default=None, min_length=1, max_length=256)
    environment: dict[str, str] | None = None
    compose: dict[str, Any] | None = None
    components: list[str] | None = None
    require_route: str | None = Field(default=None, max_length=2048)

    @field_validator("require_route")
    @classmethod
    def updated_route_is_valid(cls, value: str | None) -> str | None:
        return UserAppCreate.route_is_a_single_path(value)

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
    compose_source_file: str | None = None
    compose_source_commit: str | None = None
    components: list[str] = Field(default_factory=list)
    require_route: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UserApp(DomainModel):
    appid: str
    name: str
    repository_url: str
    git_ref: str
    environment_keys: list[str] = Field(default_factory=list)
    compose: dict[str, Any] | None = None
    compose_source_file: str | None = None
    compose_source_commit: str | None = None
    components: list[str] = Field(default_factory=list)
    require_route: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: UserAppRecord) -> "UserApp":
        return cls(**record.model_dump(exclude={"environment_ciphertext"}))


class AppSyncResult(DomainModel):
    """Outcome of synchronizing the saved Git ref into its Compose document."""

    appid: str
    git_ref: str
    commit_id: str
    compose_file: str
    compose_changed: bool
    commit_changed: bool
    previous_commit_id: str | None = None
    compose: dict[str, Any]
    updated_at: datetime


class BuildCreate(DomainModel):
    git_ref: str | None = Field(default=None, min_length=1, max_length=256)


class BuildJob(DomainModel):
    build_id: str
    appid: str
    git_ref: str
    inputs_ready: bool = True
    git_commit_sha: str | None = None
    compose_snapshot: dict[str, Any] | None = Field(default=None, repr=False)
    workspace_snapshot: str | None = None
    workspace_file_count: int = 0
    workspace_archive_sha256: str | None = None
    status: BuildStatus = BuildStatus.QUEUED
    celery_task_id: str | None = None
    jenkins_queue_url: str | None = None
    jenkins_build_number: int | None = None
    images: list[str] = Field(default_factory=list)
    service_ids: list[str] = Field(default_factory=list)
    route_check_id: str | None = None
    route_check_generation: int | None = None
    require_route_snapshot: str | None = None
    static_agent_role_version: int | None = None
    dynamic_agent_role_version: int | None = None
    route_probe_payload: dict[str, Any] | None = Field(default=None, repr=False)
    route_dynamic_role_snapshot: dict[str, Any] | None = Field(default=None, repr=False)
    static_check_passed: bool | None = None
    dynamic_check_passed: bool | None = None
    dynamic_status: Literal["pending", "queued", "running", "succeeded", "failed"] = "pending"
    dynamic_run_id: str | None = None
    dynamic_initial_started: bool = False
    dynamic_debug_started: bool = False
    dynamic_retry_started: bool = False
    dynamic_retry_payload: dict[str, Any] | None = Field(default=None, repr=False)
    dynamic_dispatch_error: str | None = None
    teardown_required: bool = False
    teardown_status: Literal["pending", "running", "succeeded", "partial_failed"] | None = None
    dynamic_report_text: str | None = None
    probe_result: dict[str, Any] | None = None
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


class BuildIdentifier(DomainModel):
    """Small app-scoped build record used by UI selectors."""

    build_id: str
    status: BuildStatus
    git_commit_sha: str | None = None


class AppIdentifiers(DomainModel):
    """Known immutable IDs for an application's UI selection controls."""

    app_id: str
    builds: list[BuildIdentifier] = Field(default_factory=list)
    commit_shas: list[str] = Field(default_factory=list)


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


class TechStackCreate(DomainModel):
    """A user-editable platform technology-stack template.

    ``line_comments`` is keyed by JSONPath-like locations (for example
    ``$.environment.PORT``). Missing paths are normalized to an empty comment;
    unknown paths are rejected so comments cannot drift from the JSON schema.
    """

    tech_stack_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    name: str = Field(min_length=1, max_length=128)
    yaml_original: str = Field(
        min_length=1,
        validation_alias=AliasChoices("yaml_original", "yaml"),
    )
    json_data: dict[str, Any] = Field(
        validation_alias=AliasChoices("json_data", "json"),
    )
    line_comments: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_and_align_documents(self) -> "TechStackCreate":
        try:
            parsed = yaml.safe_load(self.yaml_original)
        except yaml.YAMLError as exc:
            raise ValueError(f"yaml_original is invalid YAML: {exc}") from exc
        if parsed != self.json_data:
            raise ValueError("yaml_original must parse to exactly json_data")
        paths = json_structure_paths(self.json_data)
        unknown = sorted(set(self.line_comments) - set(paths))
        if unknown:
            raise ValueError(f"line_comments contains unknown JSON paths: {', '.join(unknown)}")
        self.line_comments = {path: self.line_comments.get(path, "") for path in paths}
        return self


class TechStackUpdate(DomainModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    yaml_original: str | None = Field(default=None, min_length=1, validation_alias=AliasChoices("yaml_original", "yaml"))
    json_data: dict[str, Any] | None = Field(default=None, validation_alias=AliasChoices("json_data", "json"))
    line_comments: dict[str, str] | None = None


class TechStack(DomainModel):
    tech_stack_id: str
    name: str
    yaml_original: str
    json_data: dict[str, Any]
    line_comments: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentRuntimeConfig(DomainModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    authorization_mode: Literal["never", "on-request", "untrusted"] = "never"
    sandbox_mode: Literal["danger-full-access", "workspace-write", "read-only"] = "danger-full-access"
    skip_git_repo_check: bool = True
    model: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    timeout_seconds: int = Field(default=900, gt=0, le=7200)


class AgentRole(DomainModel):
    role_key: Literal["static_build_agent", "dynamic_debug_agent"]
    prompt: str = Field(min_length=1, max_length=100000)
    output_schema: dict[str, Any]
    runtime_config: AgentRuntimeConfig = Field(default_factory=AgentRuntimeConfig)
    agent_role_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentRoleUpdate(DomainModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    prompt: str | None = Field(default=None, min_length=1, max_length=100000)
    output_schema: dict[str, Any] | None = None
    runtime_config: AgentRuntimeConfig | None = None

    @model_validator(mode="after")
    def require_update_field(self) -> "AgentRoleUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one agent role field must be provided")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Agent role configuration fields cannot be null")
        return self


class RouteCheckResult(DomainModel):
    # MongoDB persists this BigInt-like primary key as a signed BSON int64.
    # Keep the model boundary aligned with the storage type so invalid IDs
    # fail before a write rather than surfacing as a BSON encoding error.
    id: int = Field(default_factory=lambda: uuid4().int & ((1 << 63) - 1), ge=0, le=(1 << 63) - 1)
    check_id: str
    app_id: str
    commit_id: str
    require_route: str
    static_agent_role_version: int = Field(ge=1)
    dynamic_agent_role_version: int = Field(ge=1)
    generation: int = Field(default=1, ge=1)
    repository_url: str = Field(default="", repr=False)
    git_ref: str = "main"
    static_role_snapshot: dict[str, Any] = Field(default_factory=dict, repr=False)
    dynamic_role_snapshot: dict[str, Any] = Field(default_factory=dict, repr=False)
    static_task_id: str | None = None
    static_run_token: str | None = Field(default=None, repr=False)
    static_lease_expires_at: datetime | None = None
    dispatch_error: str | None = None
    dynamic_build_id: str | None = None
    dynamic_run_id: str | None = None
    static_status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    dynamic_status: Literal["pending", "queued", "running", "succeeded", "failed", "skipped"] = "pending"
    diff_list: list[str] = Field(default_factory=list)
    probe_payload: dict[str, Any] | None = None
    static_report_text: str | None = None
    static_check_passed: bool | None = None
    probe_result: dict[str, Any] | None = None
    dynamic_report_text: str | None = None
    dynamic_check_passed: bool | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class StaticCheckSubmission(DomainModel):
    check_id: str
    task_id: str | None = None
    queued: bool
    cache_hit: bool = False
    message: str
    result: RouteCheckResult


class StaticCheckRequest(DomainModel):
    model_config = ConfigDict(extra="forbid")
    commit_id: str | None = Field(default=None, min_length=7, max_length=64)

    @field_validator("commit_id")
    @classmethod
    def commit_is_full_sha(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?", value) is None:
            raise ValueError("commit_id must be a full immutable Git commit SHA")
        return value.lower() if value else None


class DynamicCheckRequest(DomainModel):
    model_config = ConfigDict(extra="forbid")
    build_id: str = Field(min_length=1, max_length=64)


class PublishedPort(DomainModel):
    """Persisted Docker port metadata used to construct external access URLs."""

    target_port: int = Field(ge=1, le=65535)
    published_port: int | None = Field(default=None, ge=1, le=65535)
    protocol: Literal["tcp", "udp", "sctp"] = "tcp"
    mode: Literal["ingress", "host"] = "ingress"


class DeploymentService(DomainModel):
    service_id: str
    appid: str
    build_id: str
    service_name: str
    image: str
    status: str
    endpoint: str | None = None
    published_ports: list[PublishedPort] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ServiceAccess(DomainModel):
    """External access information for one deployed service."""

    build_id: str
    service_name: str
    status: str
    image: str
    endpoint: str | None = None
    published_ports: list[PublishedPort] = Field(default_factory=list)
    access_urls: list[str] = Field(default_factory=list)
    access_available: bool = False
    access_reason: str | None = None


class AppAccess(DomainModel):
    """Aggregated external access information for an application."""

    appid: str
    region: str
    services: list[ServiceAccess] = Field(default_factory=list)
    access_available: bool = False
    access_urls: list[str] = Field(default_factory=list)
    access_reason: str | None = None


class BuildResourceReport(DomainModel):
    build_id: str
    appid: str
    build_status: BuildStatus
    isolated: bool
    cleanup_status: CleanupStatus
    resources: list[BuildResource] = Field(default_factory=list)
    services: list[ServiceAccess] = Field(default_factory=list)
    cleanup_allowed: bool = False
    cleanup_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    registry_storage_status: str = "not_applicable"
    reclaimed_bytes: int | None = None


class Alert(DomainModel):
    alert_id: str
    appid: str
    build_id: str | None = None
    severity: str
    message: str
    resolved_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
