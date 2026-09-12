from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from orchestrator.docker_services import DockerServiceError, DockerSwarmAdapter
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
    utc_now,
)
from orchestrator.repository import DuplicateAppError, Repository
from orchestrator.secrets import SecretBox
from orchestrator.tasks import TaskDispatcher


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
        if not isinstance(app.compose, dict) or not app.compose.get("services"):
            raise BuildInputError(
                "A user-provided Docker Compose document with services is required before creating a build"
            )
        try:
            DockerSwarmAdapter.validate_compose(app.compose)
        except DockerServiceError as exc:
            raise BuildInputError(str(exc)) from exc
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
