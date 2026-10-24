from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from datetime import datetime, timezone
import re
from threading import RLock
from typing import Any, Protocol, Sequence

from orchestrator.models import (
    ACTIVE_BUILD_STATUSES,
    Alert,
    AppEvent,
    BaseImage,
    BuildJob,
    BuildIdentifier,
    BuildResource,
    BuildResources,
    BuildStatus,
    CleanupStatus,
    DeploymentService,
    AgentRole,
    RouteCheckResult,
    TechStack,
    UserAppRecord,
    UserImage,
    utc_now,
)


class RepositoryError(RuntimeError):
    pass


class DuplicateAppError(RepositoryError):
    pass


class DuplicateTechStackError(RepositoryError):
    pass


def _app_update_fields(appid: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Keep the business identity immutable without modifying caller fields."""

    if "appid" in fields and fields["appid"] != appid:
        raise ValueError("appid is immutable")
    updates = dict(fields)
    updates.pop("appid", None)
    return updates


def _lease_expired(value: datetime | None, now: datetime) -> bool:
    if value is None:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= now


def _validate_resource_key(key: str) -> None:
    if re.fullmatch(r"[a-f0-9]{64}", key) is None:
        raise ValueError("Invalid resource key")


_ROUTE_CHECK_KEY_FIELDS = ("app_id", "commit_id", "require_route", "static_agent_role_version")
_ROUTE_CHECK_IDENTITY_FIELDS = frozenset(("check_id", "id", *_ROUTE_CHECK_KEY_FIELDS))


def _route_check_key(result: RouteCheckResult) -> dict[str, Any]:
    return {field: getattr(result, field) for field in _ROUTE_CHECK_KEY_FIELDS}


def _route_check_identity(result: RouteCheckResult) -> dict[str, Any]:
    identity = {"check_id": result.check_id, **_route_check_key(result)}
    if getattr(result, "id", None) is not None:
        identity["id"] = result.id
    return identity


def _route_check_updates(fields: dict[str, Any], expected: dict[str, Any] | None) -> dict[str, Any]:
    """Only ordinary model fields may participate in updates or CAS filters."""

    for field in fields:
        if field in _ROUTE_CHECK_IDENTITY_FIELDS:
            raise ValueError(f"{field} is immutable")
        if field not in RouteCheckResult.model_fields:
            raise ValueError(f"Unknown route check field: {field}")
    for field in expected or {}:
        if field not in RouteCheckResult.model_fields:
            raise ValueError(f"Unknown route check expectation: {field}")
    if any(isinstance(value, (Mapping, list, tuple)) for value in (expected or {}).values()):
        # Mongo interprets mappings in query values as operators while the
        # in-memory adapter compares them literally. Route-check CAS callers
        # use scalar version/status/owner tokens, so reject compound values
        # instead of allowing backend-dependent compare-and-swap semantics.
        raise ValueError("Route check expectations must be scalar values")
    updates = deepcopy(fields)
    updates.setdefault("updated_at", utc_now())
    return updates


def _sortable_datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class Repository(Protocol):
    def ensure_indexes(self) -> None: ...
    def close(self) -> None: ...
    def create_app(self, app: UserAppRecord) -> UserAppRecord: ...
    def get_app(self, appid: str) -> UserAppRecord | None: ...
    def list_apps(self) -> list[UserAppRecord]: ...
    def update_app(self, appid: str, fields: dict[str, Any]) -> UserAppRecord | None: ...
    def list_agent_roles(self) -> list[AgentRole]: ...
    def get_agent_role(self, role_key: str) -> AgentRole | None: ...
    def save_agent_role(self, role: AgentRole, expected_version: int | None = None) -> AgentRole | None: ...
    def get_codex_provider_config(self) -> dict[str, Any] | None: ...
    def save_codex_provider_config(self, config: dict[str, Any]) -> None: ...
    def get_route_check(self, check_id: str) -> RouteCheckResult | None: ...
    def find_route_check(self, app_id: str, commit_id: str, require_route: str, static_role_version: int) -> RouteCheckResult | None: ...
    def ensure_route_check(self, result: RouteCheckResult) -> tuple[RouteCheckResult, bool]: ...
    def save_route_check(self, result: RouteCheckResult) -> RouteCheckResult: ...
    def update_route_check(self, check_id: str, fields: dict[str, Any], expected: dict[str, Any] | None = None) -> RouteCheckResult | None: ...
    def delete_route_check(self, app_id: str, commit_id: str, require_route: str, static_role_version: int) -> bool: ...
    def list_route_checks(self, app_id: str, commit_id: str) -> list[RouteCheckResult]: ...
    def list_route_checks_for_app(self, app_id: str) -> list[RouteCheckResult]: ...
    def list_pending_route_checks(self) -> list[RouteCheckResult]: ...
    def create_build(self, build: BuildJob) -> BuildJob: ...
    def get_build(self, build_id: str) -> BuildJob | None: ...
    def list_builds(
        self,
        appid: str | None = None,
        status: BuildStatus | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[BuildJob], int]: ...
    def list_build_identifiers(self, appid: str) -> list[BuildIdentifier]: ...
    def list_route_check_commits(self, app_id: str) -> list[str]: ...
    def list_active_builds(self) -> list[BuildJob]: ...
    def list_pending_dynamic_builds(self) -> list[BuildJob]: ...
    def update_build(self, build_id: str, fields: dict[str, Any]) -> BuildJob | None: ...
    def transition_build(
        self, build_id: str, expected_statuses: Sequence[BuildStatus], status: BuildStatus, fields: dict[str, Any]
    ) -> BuildJob | None: ...
    def append_event(self, event: AppEvent) -> AppEvent: ...
    def list_events(self, appid: str, limit: int = 100) -> list[AppEvent]: ...
    def save_user_image(self, image: UserImage) -> UserImage: ...
    def list_user_images(self, appid: str) -> list[UserImage]: ...
    def save_base_image(self, image: BaseImage) -> BaseImage: ...
    def list_base_images(self) -> list[BaseImage]: ...
    def create_tech_stack(self, stack: TechStack) -> TechStack: ...
    def get_tech_stack(self, tech_stack_id: str) -> TechStack | None: ...
    def list_tech_stacks(self) -> list[TechStack]: ...
    def update_tech_stack(self, tech_stack_id: str, fields: dict[str, Any]) -> TechStack | None: ...
    def delete_tech_stack(self, tech_stack_id: str) -> bool: ...
    def save_service(self, service: DeploymentService) -> DeploymentService: ...
    def list_services(self, appid: str) -> list[DeploymentService]: ...
    def create_alert(self, alert: Alert) -> Alert: ...
    def list_alerts(self, appid: str) -> list[Alert]: ...
    def ensure_build_resources(self, resources: BuildResources) -> BuildResources: ...
    def get_build_resources(self, build_id: str) -> BuildResources | None: ...
    def put_build_resource(self, build_id: str, resource: BuildResource, owner: str | None = None) -> BuildResources: ...
    def update_build_resources(self, build_id: str, fields: dict[str, Any], expected: dict[str, Any] | None = None) -> BuildResources | None: ...
    def claim_build_lease(self, build_id: str, owner: str, expires_at: datetime, statuses: Sequence[CleanupStatus]) -> BuildResources | None: ...
    def renew_build_lease(self, build_id: str, owner: str, expires_at: datetime) -> bool: ...
    def release_build_lease(self, build_id: str, owner: str) -> None: ...
    def list_cleanup_resources(self) -> list[BuildResources]: ...
    def reserve_port(self, target: str, protocol: str, port: int, build_id: str, service_name: str) -> bool: ...
    def list_build_ports(self, build_id: str) -> list[dict[str, Any]]: ...
    def release_port(self, target: str, protocol: str, port: int, build_id: str) -> bool: ...


class InMemoryRepository:
    """Thread-safe development/test repository mirroring the MongoDB contract."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.apps: dict[str, UserAppRecord] = {}
        self.builds: dict[str, BuildJob] = {}
        self.events: list[AppEvent] = []
        self.user_images: dict[str, UserImage] = {}
        self.base_images: dict[str, BaseImage] = {}
        self.tech_stacks: dict[str, TechStack] = {}
        self.services: dict[str, DeploymentService] = {}
        self.alerts: dict[str, Alert] = {}
        self.build_resources: dict[str, BuildResources] = {}
        self.port_allocations: dict[tuple[str, str, int], dict[str, str]] = {}
        self.agent_roles: dict[str, AgentRole] = {}
        self.codex_provider_config: dict[str, Any] | None = None
        self.route_checks: dict[str, RouteCheckResult] = {}

    @staticmethod
    def _copy(model):
        return model.model_copy(deep=True)

    def ensure_indexes(self) -> None:
        return None

    def close(self) -> None:
        return None

    def get_codex_provider_config(self) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self.codex_provider_config)

    def save_codex_provider_config(self, config: dict[str, Any]) -> None:
        with self._lock:
            self.codex_provider_config = deepcopy(config)

    def create_app(self, app: UserAppRecord) -> UserAppRecord:
        with self._lock:
            if app.appid in self.apps:
                raise DuplicateAppError(app.appid)
            self.apps[app.appid] = self._copy(app)
            return self._copy(app)

    def get_app(self, appid: str) -> UserAppRecord | None:
        with self._lock:
            app = self.apps.get(appid)
            return self._copy(app) if app else None

    def list_apps(self) -> list[UserAppRecord]:
        with self._lock:
            values = sorted(self.apps.values(), key=lambda app: (app.name.lower(), app.appid))
            return [self._copy(app) for app in values]

    def update_app(self, appid: str, fields: dict[str, Any]) -> UserAppRecord | None:
        updates = _app_update_fields(appid, fields)
        with self._lock:
            existing = self.apps.get(appid)
            if existing is None:
                return None
            updated = existing.model_copy(update=updates, deep=True)
            self.apps[appid] = updated
            return self._copy(updated)

    def list_agent_roles(self) -> list[AgentRole]:
        with self._lock:
            return [self._copy(role) for role in sorted(self.agent_roles.values(), key=lambda role: role.role_key)]

    def get_agent_role(self, role_key: str) -> AgentRole | None:
        with self._lock:
            role = self.agent_roles.get(role_key)
            return self._copy(role) if role else None

    def save_agent_role(self, role: AgentRole, expected_version: int | None = None) -> AgentRole | None:
        with self._lock:
            current = self.agent_roles.get(role.role_key)
            if expected_version is None and current is not None:
                return self._copy(current)
            if expected_version is not None and (current is None or current.agent_role_version != expected_version):
                return None
            self.agent_roles[role.role_key] = self._copy(role)
            return self._copy(role)

    def get_route_check(self, check_id: str) -> RouteCheckResult | None:
        with self._lock:
            result = self.route_checks.get(check_id)
            return self._copy(result) if result else None

    def find_route_check(self, app_id: str, commit_id: str, require_route: str, static_role_version: int) -> RouteCheckResult | None:
        with self._lock:
            for item in self.route_checks.values():
                if (item.app_id, item.commit_id, item.require_route, item.static_agent_role_version) == (
                    app_id, commit_id, require_route, static_role_version
                ):
                    return self._copy(item)
            return None

    def ensure_route_check(self, result: RouteCheckResult) -> tuple[RouteCheckResult, bool]:
        with self._lock:
            existing = self.find_route_check(
                result.app_id, result.commit_id, result.require_route, result.static_agent_role_version
            )
            if existing is not None:
                return existing, False
            self._validate_route_check_identity(result)
            self.route_checks[result.check_id] = self._copy(result)
            return self._copy(result), True

    def _validate_route_check_identity(self, result: RouteCheckResult) -> None:
        current = self.route_checks.get(result.check_id)
        if current is not None and _route_check_identity(current) != _route_check_identity(result):
            raise ValueError("Route check identity is immutable")
        result_id = getattr(result, "id", None)
        if result_id is not None and any(
            getattr(item, "id", None) == result_id and item.check_id != result.check_id
            for item in self.route_checks.values()
        ):
            raise RepositoryError("Duplicate route check id")

    def save_route_check(self, result: RouteCheckResult) -> RouteCheckResult:
        with self._lock:
            current = self.route_checks.get(result.check_id)
            if current is not None and _route_check_identity(current) != _route_check_identity(result):
                raise ValueError("Route check identity is immutable")
            existing = self.find_route_check(
                result.app_id, result.commit_id, result.require_route, result.static_agent_role_version
            )
            if existing is not None and existing.check_id != result.check_id:
                return existing
            self._validate_route_check_identity(result)
            self.route_checks[result.check_id] = self._copy(result)
            return self._copy(result)

    def update_route_check(self, check_id: str, fields: dict[str, Any], expected: dict[str, Any] | None = None) -> RouteCheckResult | None:
        updates = _route_check_updates(fields, expected)
        with self._lock:
            current = self.route_checks.get(check_id)
            if current is None or any(getattr(current, key, None) != value for key, value in (expected or {}).items()):
                return None
            updated = current.model_copy(update=updates, deep=True)
            self.route_checks[check_id] = updated
            return self._copy(updated)

    def delete_route_check(self, app_id: str, commit_id: str, require_route: str, static_role_version: int) -> bool:
        with self._lock:
            matches = [key for key, item in self.route_checks.items() if
                       (item.app_id, item.commit_id, item.require_route, item.static_agent_role_version) ==
                       (app_id, commit_id, require_route, static_role_version)]
            if not matches:
                return False
            for key in matches:
                del self.route_checks[key]
            return True

    def list_route_checks(self, app_id: str, commit_id: str) -> list[RouteCheckResult]:
        with self._lock:
            values = [item for item in self.route_checks.values() if item.app_id == app_id and item.commit_id == commit_id]
            values.sort(key=lambda item: (_sortable_datetime(item.created_at), item.check_id), reverse=True)
            return [self._copy(item) for item in values]

    def list_route_checks_for_app(self, app_id: str) -> list[RouteCheckResult]:
        with self._lock:
            values = [item for item in self.route_checks.values() if item.app_id == app_id]
            values.sort(key=lambda item: (_sortable_datetime(item.created_at), item.check_id), reverse=True)
            return [self._copy(item) for item in values]

    def list_route_check_commits(self, app_id: str) -> list[str]:
        with self._lock:
            values = {item.commit_id for item in self.route_checks.values() if item.app_id == app_id}
            return sorted(values, reverse=True)

    def list_pending_route_checks(self) -> list[RouteCheckResult]:
        with self._lock:
            values = [item for item in self.route_checks.values() if item.static_status in {"queued", "running"}]
            values.sort(key=lambda item: (_sortable_datetime(item.updated_at), item.check_id))
            return [self._copy(item) for item in values]

    def create_build(self, build: BuildJob) -> BuildJob:
        with self._lock:
            self.builds[build.build_id] = self._copy(build)
            return self._copy(build)

    def get_build(self, build_id: str) -> BuildJob | None:
        with self._lock:
            build = self.builds.get(build_id)
            return self._copy(build) if build else None

    def list_builds(
        self,
        appid: str | None = None,
        status: BuildStatus | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[BuildJob], int]:
        if skip < 0 or limit < 1:
            raise ValueError("skip must be non-negative and limit must be positive")
        with self._lock:
            values = [
                build
                for build in self.builds.values()
                if (appid is None or build.appid == appid)
                and (status is None or build.status == status)
            ]
            values.sort(key=lambda build: (build.created_at, build.build_id), reverse=True)
            total = len(values)
            page = values[skip : skip + limit]
            return [self._copy(build) for build in page], total

    def list_build_identifiers(self, appid: str) -> list[BuildIdentifier]:
        with self._lock:
            values = [build for build in self.builds.values() if build.appid == appid]
            values.sort(key=lambda build: (build.created_at, build.build_id), reverse=True)
            return [BuildIdentifier(
                build_id=build.build_id,
                status=build.status,
                git_commit_sha=build.git_commit_sha,
            ) for build in values]

    def list_pending_dynamic_builds(self) -> list[BuildJob]:
        with self._lock:
            values = [build for build in self.builds.values()
                      if build.dynamic_status in {"queued", "running"} or build.teardown_required]
            values.sort(key=lambda build: (_sortable_datetime(build.updated_at), build.build_id))
            return [self._copy(build) for build in values]

    def list_active_builds(self) -> list[BuildJob]:
        with self._lock:
            values = [build for build in self.builds.values() if build.status in ACTIVE_BUILD_STATUSES]
            return [self._copy(build) for build in sorted(values, key=lambda build: build.updated_at)]

    def update_build(self, build_id: str, fields: dict[str, Any]) -> BuildJob | None:
        with self._lock:
            existing = self.builds.get(build_id)
            if existing is None:
                return None
            updated = existing.model_copy(update=fields, deep=True)
            self.builds[build_id] = updated
            return self._copy(updated)

    def transition_build(
        self, build_id: str, expected_statuses: Sequence[BuildStatus], status: BuildStatus, fields: dict[str, Any]
    ) -> BuildJob | None:
        with self._lock:
            existing = self.builds.get(build_id)
            if existing is None or existing.status not in set(expected_statuses):
                return None
            updated = existing.model_copy(update={**fields, "status": status}, deep=True)
            self.builds[build_id] = updated
            return self._copy(updated)

    def append_event(self, event: AppEvent) -> AppEvent:
        with self._lock:
            self.events.append(self._copy(event))
            return self._copy(event)

    def list_events(self, appid: str, limit: int = 100) -> list[AppEvent]:
        with self._lock:
            values = [event for event in self.events if event.appid == appid]
            return [self._copy(event) for event in sorted(values, key=lambda event: event.created_at, reverse=True)[:limit]]

    def save_user_image(self, image: UserImage) -> UserImage:
        with self._lock:
            self.user_images[image.image_id] = self._copy(image)
            return self._copy(image)

    def list_user_images(self, appid: str) -> list[UserImage]:
        with self._lock:
            return [self._copy(image) for image in self.user_images.values() if image.appid == appid]

    def save_base_image(self, image: BaseImage) -> BaseImage:
        with self._lock:
            self.base_images[image.source_image] = self._copy(image)
            return self._copy(image)

    def list_base_images(self) -> list[BaseImage]:
        with self._lock:
            return [self._copy(image) for image in self.base_images.values()]

    def create_tech_stack(self, stack: TechStack) -> TechStack:
        with self._lock:
            if stack.tech_stack_id in self.tech_stacks:
                raise DuplicateTechStackError(stack.tech_stack_id)
            self.tech_stacks[stack.tech_stack_id] = self._copy(stack)
            return self._copy(stack)

    def get_tech_stack(self, tech_stack_id: str) -> TechStack | None:
        with self._lock:
            stack = self.tech_stacks.get(tech_stack_id)
            return self._copy(stack) if stack else None

    def list_tech_stacks(self) -> list[TechStack]:
        with self._lock:
            values = sorted(self.tech_stacks.values(), key=lambda item: (item.name.lower(), item.tech_stack_id))
            return [self._copy(item) for item in values]

    def update_tech_stack(self, tech_stack_id: str, fields: dict[str, Any]) -> TechStack | None:
        with self._lock:
            existing = self.tech_stacks.get(tech_stack_id)
            if existing is None:
                return None
            updated = existing.model_copy(update=fields, deep=True)
            self.tech_stacks[tech_stack_id] = updated
            return self._copy(updated)

    def delete_tech_stack(self, tech_stack_id: str) -> bool:
        with self._lock:
            return self.tech_stacks.pop(tech_stack_id, None) is not None

    def save_service(self, service: DeploymentService) -> DeploymentService:
        with self._lock:
            self.services[service.service_id] = self._copy(service)
            return self._copy(service)

    def list_services(self, appid: str) -> list[DeploymentService]:
        with self._lock:
            return [self._copy(service) for service in self.services.values() if service.appid == appid]

    def create_alert(self, alert: Alert) -> Alert:
        with self._lock:
            self.alerts[alert.alert_id] = self._copy(alert)
            return self._copy(alert)

    def list_alerts(self, appid: str) -> list[Alert]:
        with self._lock:
            return [self._copy(alert) for alert in self.alerts.values() if alert.appid == appid]

    def ensure_build_resources(self, resources: BuildResources) -> BuildResources:
        with self._lock:
            existing = self.build_resources.setdefault(resources.build_id, self._copy(resources))
            if existing.appid != resources.appid:
                raise RepositoryError("Build resource owner mismatch")
            return self._copy(existing)

    def get_build_resources(self, build_id: str) -> BuildResources | None:
        with self._lock:
            resources = self.build_resources.get(build_id)
            return self._copy(resources) if resources else None

    def put_build_resource(self, build_id: str, resource: BuildResource, owner: str | None = None) -> BuildResources:
        _validate_resource_key(resource.key)
        with self._lock:
            resources = self.build_resources.get(build_id)
            if resources is None:
                raise RepositoryError("Build resources not found")
            if owner is not None and (resources.lease_owner != owner or _lease_expired(resources.lease_expires_at, utc_now())):
                raise RepositoryError("Build resource lease lost")
            resources.resources[resource.key] = self._copy(resource)
            resources.updated_at = utc_now()
            return self._copy(resources)

    def update_build_resources(self, build_id: str, fields: dict[str, Any], expected: dict[str, Any] | None = None) -> BuildResources | None:
        with self._lock:
            resources = self.build_resources.get(build_id)
            if resources is None or any(getattr(resources, key) != value for key, value in (expected or {}).items()):
                return None
            if expected and expected.get("lease_owner") is not None and _lease_expired(resources.lease_expires_at, utc_now()):
                return None
            updated = resources.model_copy(update={**fields, "updated_at": utc_now()}, deep=True)
            self.build_resources[build_id] = updated
            return self._copy(updated)

    def claim_build_lease(self, build_id: str, owner: str, expires_at: datetime, statuses: Sequence[CleanupStatus]) -> BuildResources | None:
        with self._lock:
            resources = self.build_resources.get(build_id)
            if resources is None or resources.cleanup_status not in statuses:
                return None
            if resources.lease_owner is not None and not _lease_expired(resources.lease_expires_at, utc_now()):
                return None
            resources.lease_owner = owner
            resources.lease_expires_at = expires_at
            resources.updated_at = utc_now()
            return self._copy(resources)

    def renew_build_lease(self, build_id: str, owner: str, expires_at: datetime) -> bool:
        return self.update_build_resources(build_id, {"lease_expires_at": expires_at}, {"lease_owner": owner}) is not None

    def release_build_lease(self, build_id: str, owner: str) -> None:
        with self._lock:
            resources = self.build_resources.get(build_id)
            if resources is not None and resources.lease_owner == owner:
                resources.lease_owner = None
                resources.lease_expires_at = None

    def list_cleanup_resources(self) -> list[BuildResources]:
        with self._lock:
            return [self._copy(resources) for resources in self.build_resources.values() if resources.cleanup_status in {CleanupStatus.QUEUED, CleanupStatus.RUNNING}]

    def reserve_port(self, target: str, protocol: str, port: int, build_id: str, service_name: str) -> bool:
        with self._lock:
            identity = (target, protocol, port)
            owner = {"build_id": build_id, "service_name": service_name}
            previous = self.port_allocations.setdefault(identity, owner)
            return previous == owner

    def release_port(self, target: str, protocol: str, port: int, build_id: str) -> bool:
        with self._lock:
            identity = (target, protocol, port)
            owner = self.port_allocations.get(identity)
            if owner is None:
                return True
            if owner["build_id"] != build_id:
                return False
            del self.port_allocations[identity]
            return True

    def list_build_ports(self, build_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"target": identity[0], "protocol": identity[1], "port": identity[2], **owner}
                for identity, owner in self.port_allocations.items() if owner["build_id"] == build_id
            ]


class MongoRepository:
    """MongoDB repository. The client is owned by this object when supplied."""

    def __init__(self, database, client=None) -> None:
        self._client = client
        self.apps = database["user_apps"]
        self.builds = database["build_jobs"]
        self.events = database["app_events"]
        self.user_images = database["user_images"]
        self.base_images = database["base_images"]
        self.tech_stacks = database["tech_stacks"]
        self.services = database["deployment_services"]
        self.alerts = database["alerts"]
        self.build_resources = database["build_resources"]
        self.port_allocations = database["port_allocations"]
        self.agent_roles = database["agent_roles"]
        self.system_configs = database["system_configs"]
        self.route_checks = database["route_check_list"]

    @classmethod
    def connect(cls, url: str, database_name: str, timeout_ms: int = 5000) -> "MongoRepository":
        from pymongo import MongoClient

        client = MongoClient(url, serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms)
        repository = cls(client[database_name], client=client)
        repository.ensure_indexes()
        return repository

    @staticmethod
    def _document(model) -> dict[str, Any]:
        return model.model_dump(mode="python")

    @staticmethod
    def _model(model_type, document: dict[str, Any] | None):
        if document is None:
            return None
        document = dict(document)
        document.pop("_id", None)
        return model_type.model_validate(document)

    def ensure_indexes(self) -> None:
        self.apps.create_index("appid", unique=True, name="unique_appid")
        self.builds.create_index("build_id", unique=True, name="unique_build_id")
        self.builds.create_index([("appid", 1), ("status", 1), ("created_at", -1)], name="builds_by_app_status")
        self.builds.create_index([("status", 1), ("updated_at", 1)], name="active_builds_by_status")
        self.builds.create_index([("dynamic_status", 1), ("updated_at", 1)], name="dynamic_build_recovery")
        self.builds.create_index([("teardown_required", 1), ("updated_at", 1)], name="dynamic_teardown_recovery")
        self.events.create_index([("appid", 1), ("created_at", -1)], name="events_by_app")
        self.events.create_index([("build_id", 1), ("created_at", -1)], name="events_by_build")
        self.user_images.create_index([("appid", 1), ("build_id", 1), ("reference", 1)], unique=True, name="unique_user_image")
        self.base_images.create_index("source_image", unique=True, name="unique_base_image")
        self.tech_stacks.create_index("tech_stack_id", unique=True, name="unique_tech_stack_id")
        self.services.create_index([("appid", 1), ("build_id", 1), ("service_name", 1)], unique=True, name="unique_deployment_service")
        self.alerts.create_index([("appid", 1), ("created_at", -1)], name="alerts_by_app")
        self.build_resources.create_index("build_id", unique=True, name="unique_build_resources")
        self.build_resources.create_index([("cleanup_status", 1), ("lease_expires_at", 1)], name="cleanup_recovery")
        self.port_allocations.create_index([("target", 1), ("protocol", 1), ("port", 1)], unique=True, name="unique_published_port")
        self.port_allocations.create_index("build_id", name="ports_by_build")
        self.agent_roles.create_index("role_key", unique=True, name="unique_agent_role")
        self.route_checks.create_index("check_id", unique=True, name="unique_route_check_id")
        self.route_checks.create_index("id", unique=True, name="unique_route_check_numeric_id")
        self.route_checks.create_index(
            [("app_id", 1), ("commit_id", 1), ("require_route", 1), ("static_agent_role_version", 1)],
            unique=True, name="uniq_app_commit_route_version",
        )
        self.route_checks.create_index([("app_id", 1), ("commit_id", 1), ("created_at", -1)], name="checks_by_app_commit")
        self.route_checks.create_index([("static_status", 1), ("updated_at", 1)], name="pending_static_checks")

    def list_agent_roles(self) -> list[AgentRole]:
        return [self._model(AgentRole, doc) for doc in self.agent_roles.find({}).sort("role_key", 1)]

    def get_codex_provider_config(self) -> dict[str, Any] | None:
        document = self.system_configs.find_one({"_id": "codex-provider"})
        if document is None:
            return None
        document = dict(document)
        document.pop("_id", None)
        return document

    def save_codex_provider_config(self, config: dict[str, Any]) -> None:
        self.system_configs.replace_one(
            {"_id": "codex-provider"}, {"_id": "codex-provider", **deepcopy(config)}, upsert=True,
        )

    def get_agent_role(self, role_key: str) -> AgentRole | None:
        return self._model(AgentRole, self.agent_roles.find_one({"role_key": role_key}))

    def save_agent_role(self, role: AgentRole, expected_version: int | None = None) -> AgentRole | None:
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError

        query: dict[str, Any] = {"role_key": role.role_key}
        if expected_version is not None:
            query["agent_role_version"] = expected_version
            document = self.agent_roles.find_one_and_replace(
                query, self._document(role), return_document=ReturnDocument.AFTER,
            )
            return self._model(AgentRole, document)
        try:
            document = self.agent_roles.find_one_and_update(
                query, {"$setOnInsert": self._document(role)}, upsert=True, return_document=ReturnDocument.AFTER,
            )
            return self._model(AgentRole, document)
        except DuplicateKeyError:
            # A competing worker may have initialized the role after our upsert
            # found no match. Never replace that worker's role configuration.
            existing = self.get_agent_role(role.role_key)
            if existing is None:
                raise RepositoryError("Agent role initialization raced with deletion")
            return existing

    def get_route_check(self, check_id: str) -> RouteCheckResult | None:
        return self._model(RouteCheckResult, self.route_checks.find_one({"check_id": check_id}))

    def find_route_check(self, app_id: str, commit_id: str, require_route: str, static_role_version: int) -> RouteCheckResult | None:
        document = self.route_checks.find_one({"app_id": app_id, "commit_id": commit_id,
            "require_route": require_route, "static_agent_role_version": static_role_version})
        return self._model(RouteCheckResult, document)

    def ensure_route_check(self, result: RouteCheckResult) -> tuple[RouteCheckResult, bool]:
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError

        key = _route_check_key(result)
        try:
            previous = self.route_checks.find_one_and_update(
                key, {"$setOnInsert": self._document(result)}, upsert=True, return_document=ReturnDocument.BEFORE,
            )
        except DuplicateKeyError as exc:
            existing = self._model(RouteCheckResult, self.route_checks.find_one(key))
            if existing is not None:
                return existing, False
            current = self.get_route_check(result.check_id)
            if current is not None:
                raise ValueError("Route check identity is immutable") from exc
            raise RepositoryError("Duplicate route check id") from exc
        if previous is not None:
            return self._model(RouteCheckResult, previous), False
        return self._model(RouteCheckResult, self._document(result)), True

    def save_route_check(self, result: RouteCheckResult) -> RouteCheckResult:
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError

        try:
            document = self.route_checks.find_one_and_replace(
                _route_check_identity(result), self._document(result), upsert=True, return_document=ReturnDocument.AFTER,
            )
            return self._model(RouteCheckResult, document)
        except DuplicateKeyError as exc:
            current = self.get_route_check(result.check_id)
            if current is not None and _route_check_identity(current) != _route_check_identity(result):
                raise ValueError("Route check identity is immutable") from exc
            existing = self._model(RouteCheckResult, self.route_checks.find_one(_route_check_key(result)))
            if existing is not None:
                return existing
            raise RepositoryError("Duplicate route check id") from exc

    def update_route_check(self, check_id: str, fields: dict[str, Any], expected: dict[str, Any] | None = None) -> RouteCheckResult | None:
        from pymongo import ReturnDocument

        updates = _route_check_updates(fields, expected)
        query: dict[str, Any] = {"check_id": check_id}
        if expected:
            query = {"$and": [query, deepcopy(expected)]}
        document = self.route_checks.find_one_and_update(
            query, {"$set": updates}, return_document=ReturnDocument.AFTER,
        )
        return self._model(RouteCheckResult, document)

    def delete_route_check(self, app_id: str, commit_id: str, require_route: str, static_role_version: int) -> bool:
        return self.route_checks.delete_one({"app_id": app_id, "commit_id": commit_id,
            "require_route": require_route, "static_agent_role_version": static_role_version}).deleted_count == 1

    def list_route_checks(self, app_id: str, commit_id: str) -> list[RouteCheckResult]:
        return [self._model(RouteCheckResult, doc) for doc in self.route_checks.find(
            {"app_id": app_id, "commit_id": commit_id}).sort([("created_at", -1), ("check_id", -1)])]

    def list_route_checks_for_app(self, app_id: str) -> list[RouteCheckResult]:
        return [self._model(RouteCheckResult, doc) for doc in self.route_checks.find(
            {"app_id": app_id}).sort([("created_at", -1), ("check_id", -1)])]

    def list_route_check_commits(self, app_id: str) -> list[str]:
        return sorted(self.route_checks.distinct("commit_id", {"app_id": app_id}), reverse=True)

    def list_pending_route_checks(self) -> list[RouteCheckResult]:
        return [self._model(RouteCheckResult, doc) for doc in self.route_checks.find(
            {"static_status": {"$in": ["queued", "running"]}}).sort([("updated_at", 1), ("check_id", 1)])]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def create_app(self, app: UserAppRecord) -> UserAppRecord:
        try:
            self.apps.insert_one(self._document(app))
        except Exception as exc:
            if getattr(exc, "code", None) == 11000:
                raise DuplicateAppError(app.appid) from exc
            raise RepositoryError("Unable to create user-app") from exc
        return app

    def get_app(self, appid: str) -> UserAppRecord | None:
        return self._model(UserAppRecord, self.apps.find_one({"appid": appid}))

    def list_apps(self) -> list[UserAppRecord]:
        documents = self.apps.find({}).sort([("name", 1), ("appid", 1)])
        return [self._model(UserAppRecord, document) for document in documents]

    def update_app(self, appid: str, fields: dict[str, Any]) -> UserAppRecord | None:
        updates = _app_update_fields(appid, fields)
        from pymongo import ReturnDocument

        document = self.apps.find_one_and_update(
            {"appid": appid}, {"$set": updates}, return_document=ReturnDocument.AFTER
        )
        return self._model(UserAppRecord, document)

    def create_build(self, build: BuildJob) -> BuildJob:
        try:
            self.builds.insert_one(self._document(build))
        except Exception as exc:
            raise RepositoryError("Unable to create build job") from exc
        return build

    def get_build(self, build_id: str) -> BuildJob | None:
        return self._model(BuildJob, self.builds.find_one({"build_id": build_id}))

    def list_builds(
        self,
        appid: str | None = None,
        status: BuildStatus | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[BuildJob], int]:
        if skip < 0 or limit < 1:
            raise ValueError("skip must be non-negative and limit must be positive")
        query: dict[str, Any] = {}
        if appid is not None:
            query["appid"] = appid
        if status is not None:
            query["status"] = status.value
        cursor = self.builds.find(query).sort([("created_at", -1), ("build_id", -1)])
        total = self.builds.count_documents(query)
        documents = cursor.skip(skip).limit(limit)
        return [self._model(BuildJob, document) for document in documents], total

    def list_build_identifiers(self, appid: str) -> list[BuildIdentifier]:
        documents = self.builds.find(
            {"appid": appid}, {"_id": 0, "build_id": 1, "status": 1, "git_commit_sha": 1}
        ).sort([("created_at", -1), ("build_id", -1)])
        return [BuildIdentifier.model_validate(document) for document in documents]

    def list_active_builds(self) -> list[BuildJob]:
        statuses = [status.value for status in ACTIVE_BUILD_STATUSES]
        return [
            self._model(BuildJob, document)
            for document in self.builds.find({"status": {"$in": statuses}}).sort("updated_at", 1)
        ]

    def list_pending_dynamic_builds(self) -> list[BuildJob]:
        return [self._model(BuildJob, document) for document in self.builds.find({
            "$or": [{"dynamic_status": {"$in": ["queued", "running"]}}, {"teardown_required": True}],
        }).sort([("updated_at", 1), ("build_id", 1)])]

    def update_build(self, build_id: str, fields: dict[str, Any]) -> BuildJob | None:
        from pymongo import ReturnDocument

        document = self.builds.find_one_and_update(
            {"build_id": build_id}, {"$set": fields}, return_document=ReturnDocument.AFTER
        )
        return self._model(BuildJob, document)

    def transition_build(
        self, build_id: str, expected_statuses: Sequence[BuildStatus], status: BuildStatus, fields: dict[str, Any]
    ) -> BuildJob | None:
        from pymongo import ReturnDocument

        document = self.builds.find_one_and_update(
            {"build_id": build_id, "status": {"$in": [item.value for item in expected_statuses]}},
            {"$set": {**fields, "status": status.value}},
            return_document=ReturnDocument.AFTER,
        )
        return self._model(BuildJob, document)

    def append_event(self, event: AppEvent) -> AppEvent:
        self.events.insert_one(self._document(event))
        return event

    def list_events(self, appid: str, limit: int = 100) -> list[AppEvent]:
        return [self._model(AppEvent, document) for document in self.events.find({"appid": appid}).sort("created_at", -1).limit(limit)]

    def save_user_image(self, image: UserImage) -> UserImage:
        self.user_images.replace_one(
            {"appid": image.appid, "build_id": image.build_id, "reference": image.reference}, self._document(image), upsert=True
        )
        return image

    def list_user_images(self, appid: str) -> list[UserImage]:
        return [self._model(UserImage, document) for document in self.user_images.find({"appid": appid}).sort("created_at", -1)]

    def save_base_image(self, image: BaseImage) -> BaseImage:
        self.base_images.replace_one({"source_image": image.source_image}, self._document(image), upsert=True)
        return image

    def list_base_images(self) -> list[BaseImage]:
        return [self._model(BaseImage, document) for document in self.base_images.find({}).sort("source_image", 1)]

    def create_tech_stack(self, stack: TechStack) -> TechStack:
        try:
            self.tech_stacks.insert_one(self._document(stack))
        except Exception as exc:
            if getattr(exc, "code", None) == 11000:
                raise DuplicateTechStackError(stack.tech_stack_id) from exc
            raise RepositoryError("Unable to create technology stack") from exc
        return stack

    def get_tech_stack(self, tech_stack_id: str) -> TechStack | None:
        return self._model(TechStack, self.tech_stacks.find_one({"tech_stack_id": tech_stack_id}))

    def list_tech_stacks(self) -> list[TechStack]:
        documents = self.tech_stacks.find({}).sort([("name", 1), ("tech_stack_id", 1)])
        return [self._model(TechStack, document) for document in documents]

    def update_tech_stack(self, tech_stack_id: str, fields: dict[str, Any]) -> TechStack | None:
        from pymongo import ReturnDocument

        document = self.tech_stacks.find_one_and_update(
            {"tech_stack_id": tech_stack_id}, {"$set": fields}, return_document=ReturnDocument.AFTER
        )
        return self._model(TechStack, document)

    def delete_tech_stack(self, tech_stack_id: str) -> bool:
        return self.tech_stacks.delete_one({"tech_stack_id": tech_stack_id}).deleted_count == 1

    def save_service(self, service: DeploymentService) -> DeploymentService:
        self.services.replace_one(
            {"appid": service.appid, "build_id": service.build_id, "service_name": service.service_name},
            self._document(service),
            upsert=True,
        )
        return service

    def list_services(self, appid: str) -> list[DeploymentService]:
        return [self._model(DeploymentService, document) for document in self.services.find({"appid": appid}).sort("created_at", -1)]

    def create_alert(self, alert: Alert) -> Alert:
        self.alerts.insert_one(self._document(alert))
        return alert

    def list_alerts(self, appid: str) -> list[Alert]:
        return [self._model(Alert, document) for document in self.alerts.find({"appid": appid}).sort("created_at", -1)]

    def ensure_build_resources(self, resources: BuildResources) -> BuildResources:
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError

        try:
            document = self.build_resources.find_one_and_update(
                {"build_id": resources.build_id},
                {"$setOnInsert": self._document(resources)},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            document = self.build_resources.find_one({"build_id": resources.build_id})
        result = self._model(BuildResources, document)
        if result is None or result.appid != resources.appid:
            raise RepositoryError("Build resource owner mismatch")
        return result

    def get_build_resources(self, build_id: str) -> BuildResources | None:
        return self._model(BuildResources, self.build_resources.find_one({"build_id": build_id}))

    def put_build_resource(self, build_id: str, resource: BuildResource, owner: str | None = None) -> BuildResources:
        from pymongo import ReturnDocument

        _validate_resource_key(resource.key)
        query: dict[str, Any] = {"build_id": build_id}
        if owner is not None:
            query.update({"lease_owner": owner, "lease_expires_at": {"$gt": utc_now()}})
        document = self.build_resources.find_one_and_update(
            query,
            {"$set": {f"resources.{resource.key}": self._document(resource), "updated_at": utc_now()}},
            return_document=ReturnDocument.AFTER,
        )
        result = self._model(BuildResources, document)
        if result is None:
            raise RepositoryError("Build resources not found or lease lost")
        return result

    def update_build_resources(self, build_id: str, fields: dict[str, Any], expected: dict[str, Any] | None = None) -> BuildResources | None:
        from pymongo import ReturnDocument

        query = {"build_id": build_id, **(expected or {})}
        if expected and expected.get("lease_owner") is not None:
            query["lease_expires_at"] = {"$gt": utc_now()}
        document = self.build_resources.find_one_and_update(
            query,
            {"$set": {**fields, "updated_at": utc_now()}},
            return_document=ReturnDocument.AFTER,
        )
        return self._model(BuildResources, document)

    def claim_build_lease(self, build_id: str, owner: str, expires_at: datetime, statuses: Sequence[CleanupStatus]) -> BuildResources | None:
        from pymongo import ReturnDocument

        document = self.build_resources.find_one_and_update(
            {
                "build_id": build_id,
                "cleanup_status": {"$in": [status.value for status in statuses]},
                "$or": [{"lease_owner": None}, {"lease_expires_at": {"$lte": utc_now()}}],
            },
            {"$set": {"lease_owner": owner, "lease_expires_at": expires_at, "updated_at": utc_now()}},
            return_document=ReturnDocument.AFTER,
        )
        return self._model(BuildResources, document)

    def renew_build_lease(self, build_id: str, owner: str, expires_at: datetime) -> bool:
        return self.update_build_resources(build_id, {"lease_expires_at": expires_at}, {"lease_owner": owner}) is not None

    def release_build_lease(self, build_id: str, owner: str) -> None:
        self.build_resources.update_one({"build_id": build_id, "lease_owner": owner}, {"$set": {"lease_owner": None, "lease_expires_at": None}})

    def list_cleanup_resources(self) -> list[BuildResources]:
        return [
            self._model(BuildResources, document)
            for document in self.build_resources.find({"cleanup_status": {"$in": [CleanupStatus.QUEUED.value, CleanupStatus.RUNNING.value]}})
        ]

    def reserve_port(self, target: str, protocol: str, port: int, build_id: str, service_name: str) -> bool:
        from pymongo.errors import DuplicateKeyError

        identity = {"target": target, "protocol": protocol, "port": port}
        try:
            self.port_allocations.insert_one({**identity, "build_id": build_id, "service_name": service_name})
            return True
        except DuplicateKeyError:
            existing = self.port_allocations.find_one(identity)
            return existing is not None and existing.get("build_id") == build_id and existing.get("service_name") == service_name

    def release_port(self, target: str, protocol: str, port: int, build_id: str) -> bool:
        identity = {"target": target, "protocol": protocol, "port": port}
        self.port_allocations.delete_one({**identity, "build_id": build_id})
        return self.port_allocations.find_one(identity) is None

    def list_build_ports(self, build_id: str) -> list[dict[str, Any]]:
        return [{key: value for key, value in document.items() if key != "_id"}
                for document in self.port_allocations.find({"build_id": build_id})]
