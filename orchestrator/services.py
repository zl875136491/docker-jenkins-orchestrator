from __future__ import annotations

from datetime import datetime
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import uuid4

import yaml

from orchestrator.models import (
    Alert,
    AppEvent,
    BuildCreate,
    BuildJob,
    BuildStatus,
    EventLevel,
    TERMINAL_BUILD_STATUSES,
    UserApp,
    UserAppCreate,
    UserAppRecord,
    UserAppUpdate,
    TechStack,
    TechStackCreate,
    TechStackUpdate,
    json_structure_paths,
    utc_now,
)
from orchestrator.repository import DuplicateAppError, DuplicateTechStackError, Repository
from orchestrator.secrets import SecretBox
from orchestrator.tasks import TaskDispatcher
from orchestrator.templates import TemplateError


class NotFoundError(LookupError):
    pass


class BuildInputError(ValueError):
    pass


class InvalidBuildTransition(ValueError):
    pass


ALLOWED_BUILD_TRANSITIONS: dict[BuildStatus, set[BuildStatus]] = {
    BuildStatus.QUEUED: {BuildStatus.VALIDATING, BuildStatus.FAILED, BuildStatus.CANCELLED},
    BuildStatus.VALIDATING: {BuildStatus.TRIGGERING, BuildStatus.FAILED, BuildStatus.CANCELLED},
    BuildStatus.TRIGGERING: {BuildStatus.BUILDING, BuildStatus.FAILED, BuildStatus.CANCELLED},
    BuildStatus.BUILDING: {BuildStatus.DEPLOYING, BuildStatus.FAILED, BuildStatus.CANCELLED},
    BuildStatus.DEPLOYING: {BuildStatus.SUCCEEDED, BuildStatus.FAILED, BuildStatus.CANCELLED},
    BuildStatus.SUCCEEDED: set(),
    BuildStatus.FAILED: set(),
    BuildStatus.CANCELLED: set(),
}


class ApplicationService:
    def __init__(self, repository: Repository, secret_box: SecretBox) -> None:
        self.repository = repository
        self.secret_box = secret_box

    @staticmethod
    def _public(record: UserAppRecord) -> UserApp:
        return UserApp.from_record(record)

    def create_app(self, request: UserAppCreate) -> UserApp:
        now = utc_now()
        record = UserAppRecord(
            appid=request.appid,
            name=request.name,
            repository_url=request.repository_url,
            git_ref=request.git_ref,
            environment_ciphertext=self.secret_box.encrypt_environment(request.environment),
            environment_keys=sorted(request.environment),
            compose=request.compose,
            components=request.components,
            created_at=now,
            updated_at=now,
        )
        return self._public(self.repository.create_app(record))

    def get_record(self, appid: str) -> UserAppRecord:
        app = self.repository.get_app(appid)
        if app is None:
            raise NotFoundError("App not found")
        return app

    def get_app(self, appid: str) -> UserApp:
        return self._public(self.get_record(appid))

    def list_apps(self) -> list[UserApp]:
        return [self._public(record) for record in self.repository.list_apps()]

    def get_environment(self, appid: str) -> dict[str, str]:
        return self.secret_box.decrypt_environment(self.get_record(appid).environment_ciphertext)

    def update_app(self, appid: str, request: UserAppUpdate) -> UserApp:
        self.get_record(appid)
        fields = request.model_dump(exclude_unset=True)
        if "environment" in fields:
            environment = fields.pop("environment")
            if environment is not None:
                fields["environment_ciphertext"] = self.secret_box.encrypt_environment(environment)
                fields["environment_keys"] = sorted(environment)
        fields["updated_at"] = utc_now()
        updated = self.repository.update_app(appid, fields)
        if updated is None:
            raise NotFoundError("App not found")
        return self._public(updated)


class TechStackService:
    """Persist and project editable stack records into the compose catalog."""

    def __init__(self, repository: Repository, catalog) -> None:
        self.repository = repository
        self.catalog = catalog
        self._synchronize_catalog()

    @staticmethod
    def _comments(json_data: dict[str, Any], comments: Mapping[str, str] | None) -> dict[str, str]:
        comments = comments or {}
        paths = json_structure_paths(json_data)
        unknown = sorted(set(comments) - set(paths))
        if unknown:
            raise TemplateError(f"line_comments contains unknown JSON paths: {', '.join(unknown)}")
        return {path: str(comments.get(path, "")) for path in paths}

    def _record(self, payload: TechStackCreate, now: datetime | None = None) -> tuple[TechStack, dict[str, Any]]:
        normalized = self.catalog.validate_component(payload.tech_stack_id, payload.json_data)
        # Store the caller's YAML/JSON pair verbatim; the catalog receives the
        # normalized component so it can render reliable Compose documents.
        return TechStack(
            tech_stack_id=payload.tech_stack_id,
            name=payload.name,
            yaml_original=payload.yaml_original,
            json_data=deepcopy(payload.json_data),
            line_comments=self._comments(payload.json_data, payload.line_comments),
            created_at=now or utc_now(),
            updated_at=now or utc_now(),
        ), normalized

    def _synchronize_catalog(self) -> None:
        records = self.repository.list_tech_stacks()
        if not records:
            seed: list[TechStack] = []
            for name in self.catalog.names():
                component = deepcopy(self.catalog.components[name])
                yaml_original = self.catalog.component_yaml(name)
                seed.append(
                    TechStack(
                        tech_stack_id=name,
                        name=name,
                        yaml_original=yaml_original,
                        json_data=component,
                        line_comments={path: "" for path in json_structure_paths(component)},
                    )
                )
            for record in seed:
                self.repository.create_tech_stack(record)
            records = seed
        self.catalog.load_components({record.tech_stack_id: record.json_data for record in records})

    def list(self) -> list[TechStack]:
        return self.repository.list_tech_stacks()

    def get(self, tech_stack_id: str) -> TechStack:
        record = self.repository.get_tech_stack(tech_stack_id)
        if record is None:
            raise NotFoundError("Technology stack not found")
        return record

    def create(self, payload: TechStackCreate) -> TechStack:
        record, normalized = self._record(payload)
        try:
            created = self.repository.create_tech_stack(record)
        except DuplicateTechStackError:
            raise
        self.catalog.upsert_component(payload.tech_stack_id, normalized)
        return created

    def update(self, tech_stack_id: str, payload: TechStackUpdate) -> TechStack:
        existing = self.get(tech_stack_id)
        fields = payload.model_dump(exclude_unset=True, by_alias=False)
        if fields.get("name") is None:
            fields.pop("name", None)
        yaml_original = fields.pop("yaml_original", None)
        json_data = fields.pop("json_data", None)
        comments = fields.pop("line_comments", None)
        if yaml_original is None and json_data is not None:
            yaml_original = yaml.safe_dump(json_data, allow_unicode=False, sort_keys=False)
        if json_data is None and yaml_original is not None:
            try:
                json_data = yaml.safe_load(yaml_original)
            except yaml.YAMLError as exc:
                raise TemplateError(f"yaml_original is invalid YAML: {exc}") from exc
        if yaml_original is not None or json_data is not None or comments is not None:
            yaml_original = yaml_original if yaml_original is not None else existing.yaml_original
            json_data = json_data if json_data is not None else existing.json_data
            candidate = TechStackCreate(
                tech_stack_id=tech_stack_id,
                name=str(fields.get("name", existing.name)),
                yaml_original=yaml_original,
                json_data=json_data,
                line_comments=comments if comments is not None else existing.line_comments,
            )
            _, normalized = self._record(candidate, now=existing.updated_at)
            fields.update(
                yaml_original=candidate.yaml_original,
                json_data=deepcopy(candidate.json_data),
                line_comments=candidate.line_comments,
            )
        if "name" not in fields and not (yaml_original is not None or json_data is not None or comments is not None):
            fields["name"] = existing.name
        fields["updated_at"] = utc_now()
        updated = self.repository.update_tech_stack(tech_stack_id, fields)
        if updated is None:
            raise NotFoundError("Technology stack not found")
        if yaml_original is not None or json_data is not None or comments is not None:
            self.catalog.upsert_component(tech_stack_id, normalized)
        if yaml_original is None and json_data is None and comments is None:
            self.catalog.upsert_component(tech_stack_id, self.catalog.components[tech_stack_id])
        return updated

    def delete(self, tech_stack_id: str) -> None:
        self.get(tech_stack_id)
        if not self.repository.delete_tech_stack(tech_stack_id):
            raise NotFoundError("Technology stack not found")
        self.catalog.remove_component(tech_stack_id)


class BuildService:
    def __init__(self, repository: Repository, application_service: ApplicationService, dispatcher: TaskDispatcher) -> None:
        self.repository = repository
        self.application_service = application_service
        self.dispatcher = dispatcher

    def _event(
        self, appid: str, build_id: str | None, kind: str, message: str, level: EventLevel = EventLevel.INFO, **details: Any
    ) -> None:
        self.repository.append_event(
            AppEvent(
                event_id=uuid4().hex,
                appid=appid,
                build_id=build_id,
                kind=kind,
                level=level,
                message=message,
                details=details,
            )
        )

    def record_event(
        self,
        appid: str,
        build_id: str | None,
        kind: str,
        message: str,
        level: EventLevel = EventLevel.INFO,
        **details: Any,
    ) -> None:
        """Persist a safe operational event from an asynchronous worker."""

        self._event(appid, build_id, kind, message, level, **details)

    def queue_build(self, appid: str, request: BuildCreate) -> BuildJob:
        app = self.application_service.get_record(appid)
        if not isinstance(app.compose, Mapping):
            raise BuildInputError(
                "A user-provided Docker Compose document with services is required before creating a build"
            )
        services = app.compose.get("services")
        if not isinstance(services, Mapping) or not services or not all(
            isinstance(name, str) and isinstance(service, Mapping) for name, service in services.items()
        ):
            raise BuildInputError("A user-provided Docker Compose document with services is required before creating a build")
        build = BuildJob(build_id=uuid4().hex, appid=appid, git_ref=request.git_ref or app.git_ref)
        self.repository.create_build(build)
        self._event(appid, build.build_id, "build.queued", "Build queued")
        try:
            submission = self.dispatcher.dispatch_build(build.build_id)
        except Exception as exc:
            self.fail(build.build_id, "Unable to enqueue build task")
            raise BuildInputError("Unable to enqueue build task") from exc
        updated = self.repository.update_build(build.build_id, {"celery_task_id": submission.task_id, "updated_at": utc_now()})
        if updated is None:
            raise NotFoundError("Build not found after queueing")
        self._event(appid, build.build_id, "build.dispatched", "Build task dispatched", task_id=submission.task_id)
        return updated

    def get_build(self, build_id: str) -> BuildJob:
        build = self.repository.get_build(build_id)
        if build is None:
            raise NotFoundError("Build not found")
        return build

    def transition(self, build_id: str, target: BuildStatus, **fields: Any) -> BuildJob:
        build = self.get_build(build_id)
        if build.status == target:
            return build
        if build.status in TERMINAL_BUILD_STATUSES or target not in ALLOWED_BUILD_TRANSITIONS[build.status]:
            raise InvalidBuildTransition(f"Cannot transition build from {build.status.value} to {target.value}")
        now = utc_now()
        fields = {**fields, "updated_at": now}
        if target == BuildStatus.VALIDATING and build.started_at is None:
            fields["started_at"] = now
        if target in TERMINAL_BUILD_STATUSES:
            fields["finished_at"] = now
        updated = self.repository.transition_build(build_id, [build.status], target, fields)
        if updated is None:
            current = self.get_build(build_id)
            if current.status == target:
                return current
            raise InvalidBuildTransition("Concurrent build status update")
        level = EventLevel.ERROR if target == BuildStatus.FAILED else EventLevel.INFO
        self._event(updated.appid, build_id, f"build.{target.value}", f"Build is {target.value}", level)
        return updated

    def fail(self, build_id: str, message: str) -> BuildJob:
        build = self.get_build(build_id)
        if build.status == BuildStatus.FAILED:
            return build
        if build.status in TERMINAL_BUILD_STATUSES:
            return build
        failed = self.transition(build_id, BuildStatus.FAILED, error=message)
        self.repository.create_alert(
            Alert(
                alert_id=uuid4().hex,
                appid=failed.appid,
                build_id=failed.build_id,
                severity="error",
                message=message,
            )
        )
        self._event(failed.appid, failed.build_id, "build.alert", message, EventLevel.ERROR)
        return failed
