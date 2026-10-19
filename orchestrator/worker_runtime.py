"""Durable Celery worker orchestration for builds and base images.

The runtime keeps external clients injectable so the full lifecycle can be
tested without contacting GitLab, Jenkins, Harbor, or a Docker daemon.  It
only exchanges identifiers through Celery; all meaningful state is persisted
through the configured repository.
"""

from __future__ import annotations

import hashlib
import re
import socket
import time
from contextvars import ContextVar
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from jose import jwt

from orchestrator.config import Settings, get_settings
from orchestrator.docker_services import DockerServiceError, DockerSwarmAdapter
from orchestrator.gitlab import GitLabError, GitLabHttpAdapter
from orchestrator.jenkins import JenkinsBuildRequest, JenkinsBuildState, JenkinsError, JenkinsHttpAdapter, JenkinsNotStartedError
from orchestrator.models import BaseImage, BuildJob, BuildResources, BuildStatus, CleanupStatus, EventLevel, ResourceState, UserAppRecord, UserImage, utc_now
from orchestrator.naming import NamingError, external_namespace, is_docker_repository_component, validate_image_repository
from orchestrator.registry import DockerRegistrySynchronizer, RegistrySyncError
from orchestrator.services import BuildInputError, BuildService, NotFoundError
from orchestrator.templates import TemplateCatalog, TemplateError
from orchestrator.workspaces import WorkspaceStore
from orchestrator.build_resources import build_lease, ResourceConflict


_SYSTEM_APPID = "_system"
_TERMINAL_STATUSES = {BuildStatus.SUCCEEDED, BuildStatus.FAILED, BuildStatus.CANCELLED}


class WorkerRuntime:
    """Advance build records through the Jenkins and Docker lifecycle."""

    def __init__(
        self,
        settings: Settings,
        *,
        repository: Any | None = None,
        build_service: BuildService | None = None,
        catalog: TemplateCatalog | None = None,
        gitlab: Any | None = None,
        jenkins: Any | None = None,
        registry: Any | None = None,
        docker_services: Any | None = None,
        start_scheduler: Callable[[str], Any] | None = None,
        poll_scheduler: Callable[[str, int, int], Any] | None = None,
    ) -> None:
        self.settings = settings
        self._repository = repository
        self._build_service = build_service
        self._catalog = catalog
        self._gitlab = gitlab
        self._jenkins = jenkins
        self._registry = registry
        self._docker_services = docker_services
        self._start_scheduler = start_scheduler
        self._poll_scheduler = poll_scheduler
        self.workspace = WorkspaceStore.from_settings(settings)
        self._operation_tracker = ContextVar("build_resource_tracker", default=None)

    def _ensure_dependencies(self) -> None:
        if self._repository is not None and self._build_service is not None and self._catalog is not None:
            return
        from orchestrator.container import create_container

        container = create_container(self.settings)
        self._repository = container.repository
        self._build_service = container.builds
        self._catalog = container.catalog

    @property
    def repository(self):
        self._ensure_dependencies()
        return self._repository

    @property
    def builds(self) -> BuildService:
        self._ensure_dependencies()
        return self._build_service

    @property
    def catalog(self) -> TemplateCatalog:
        self._ensure_dependencies()
        return self._catalog

    def _gitlab_adapter(self):
        if self._gitlab is None and self.settings.gitlab_url:
            self._gitlab = GitLabHttpAdapter.from_settings(self.settings)
        return self._gitlab

    def _jenkins_adapter(self):
        if self._jenkins is None:
            self._jenkins = JenkinsHttpAdapter.from_settings(self.settings)
        return self._jenkins

    def _registry_adapter(self):
        if self._registry is None:
            self._registry = DockerRegistrySynchronizer.from_settings(self.settings)
        return self._registry

    def _docker_services_adapter(self):
        if self._docker_services is None:
            self._docker_services = DockerSwarmAdapter.from_settings(self.settings)
        return self._docker_services

    def start_build(self, build_id: str, task_id: str | None) -> dict[str, str]:
        return self._run_build_operation(build_id, lambda: self._start_build(build_id, task_id))

    def poll_build(self, build_id: str, attempt: int, task_id: str | None) -> dict[str, str]:
        return self._run_build_operation(build_id, lambda: self._poll_build(build_id, attempt, task_id))

    def _run_build_operation(self, build_id: str, operation: Callable) -> dict[str, str]:
        build = self.builds.get_build(build_id)
        if build.status in _TERMINAL_STATUSES or not build.inputs_ready:
            return _result(build)
        self.repository.ensure_build_resources(BuildResources(build_id=build_id, appid=build.appid, ownership_version=0))
        with build_lease(self.repository, build_id, [CleanupStatus.NOT_STARTED]) as tracker:
            if tracker is None:
                return _result(self.builds.get_build(build_id))
            token = self._operation_tracker.set(tracker)
            try:
                tracker.guard()
                return operation()
            except ResourceConflict:
                try:
                    tracker.guard()
                except ResourceConflict:
                    return _result(self.builds.get_build(build_id))
                raise
            finally:
                self._operation_tracker.reset(token)

    def _guard_operation(self) -> None:
        tracker = self._operation_tracker.get()
        if tracker is not None:
            tracker.guard()

    def _start_build(self, build_id: str, task_id: str | None) -> dict[str, str]:
        """Validate input, trigger Jenkins once, and schedule a durable poll."""

        build = self.builds.get_build(build_id)
        if build.status in _TERMINAL_STATUSES:
            return _result(build)
        app = self._application(build)
        compose = build.compose_snapshot if build.compose_snapshot is not None else app.compose

        try:
            self._guard_operation()
            if build.celery_task_id is None and task_id:
                build = self.repository.update_build(build_id, {"celery_task_id": task_id, "updated_at": utc_now()}) or build
            if build.status is BuildStatus.QUEUED:
                build = self.builds.transition(build_id, BuildStatus.VALIDATING)

            if build.status is BuildStatus.VALIDATING:
                runtime_compose = self._workspace_compose(build, compose)
                self._validate_source_compose(runtime_compose)
                if self._isolated(build):
                    self._docker_validation_adapter().validate_build_compose(
                        runtime_compose,
                        environment=self.builds.application_service.secret_box.decrypt_environment(app.environment_ciphertext),
                    )
                    if any(len(f"{build.build_id}-{name}") > 128 for name in compose["services"]):
                        raise BuildInputError("Build-exclusive image tags must fit Docker's 128-character limit")
                build_source = self.workspace.build_source(compose)
                if build_source == "git" and not app.repository_url and any(service.get("build") is not None for service in compose["services"].values()):
                    raise BuildInputError("A repository_url is required for git builds; use x-orchestrator-build-source: workspace for uploaded sources")
                self.builds.record_event(app.appid, build_id, "build.compose_validated", "Build compose input validated")
                git_ref = build.git_ref
                gitlab = self._gitlab_adapter()
                if gitlab is not None and app.repository_url and build_source != "workspace" and build.git_commit_sha is None:
                    resolved = gitlab.validate_repository_ref(app.repository_url, build.git_ref)
                    self._guard_operation()
                    git_ref = resolved.commit_sha
                    build = self.repository.update_build(
                        build_id,
                        {"git_commit_sha": resolved.commit_sha, "updated_at": utc_now()},
                    ) or build
                    self.builds.record_event(
                        app.appid,
                        build_id,
                        "build.gitlab_ref_validated",
                        "GitLab repository reference validated",
                        git_ref=build.git_ref,
                        commit_sha=resolved.commit_sha,
                    )
                git_ref = build.git_commit_sha or git_ref
                self._guard_operation()
                build = self.builds.transition(build_id, BuildStatus.TRIGGERING)
            else:
                git_ref = build.git_commit_sha or build.git_ref

            if build.status is BuildStatus.TRIGGERING and not build.jenkins_queue_url:
                tracker = self._operation_tracker.get()
                tracker.guard()
                manifest = self.repository.get_build_resources(build_id)
                if manifest.jenkins_trigger_pending:
                    raise BuildInputError("Jenkins trigger outcome is unknown; verify external execution before retrying")
                if self._isolated(build):
                    for name in compose["services"]:
                        tracker.record("registry_image", f"{self._image_repository(app.appid)}:{build.build_id}-{name}", target="harbor")
                request = JenkinsBuildRequest(
                    appid=app.appid,
                    repository_url=app.repository_url,
                    git_ref=git_ref,
                    environment=self.builds.application_service.secret_box.decrypt_environment(app.environment_ciphertext),
                    compose=compose or {},
                    image_repository=self._image_repository(app.appid),
                    workspace_archive_url=self._workspace_archive_url(build, compose),
                    workspace_archive_sha256=build.workspace_archive_sha256 or "",
                    workspace_archive_token=self._workspace_archive_token(build),
                    build_id=build.build_id if self._isolated(build) else "",
                )
                request.as_parameters()
                jenkins = self._jenkins_adapter()
                tracker.guard()
                self.repository.update_build_resources(build_id, {"jenkins_trigger_pending": True}, {"lease_owner": tracker.owner})
                try:
                    queued = jenkins.trigger_build(request)
                except JenkinsNotStartedError:
                    tracker.guard()
                    self.repository.update_build_resources(build_id, {"jenkins_trigger_pending": False}, {"lease_owner": tracker.owner})
                    raise
                tracker.guard()
                build = self.repository.update_build(
                    build_id,
                    {"jenkins_queue_url": queued.queue_url, "updated_at": utc_now()},
                ) or build
                self.repository.update_build_resources(build_id, {"jenkins_trigger_pending": False}, {"lease_owner": tracker.owner})
                self.builds.record_event(
                    app.appid,
                    build_id,
                    "build.jenkins_triggered",
                    "Jenkins build triggered",
                    queue_id=queued.queue_id,
                )

            if build.status is BuildStatus.TRIGGERING:
                self._guard_operation()
                build = self.builds.transition(build_id, BuildStatus.BUILDING)
            if build.status in {BuildStatus.BUILDING, BuildStatus.DEPLOYING}:
                self._schedule_poll(build_id, 0)
            return _result(build)
        except Exception as exc:
            failed = self._fail(build, exc, "Build validation or Jenkins trigger failed")
            return _result(failed)

    def _poll_build(self, build_id: str, attempt: int, task_id: str | None) -> dict[str, str]:
        """Poll Jenkins or resume deployment after a worker retry/restart."""

        build = self.builds.get_build(build_id)
        if build.status in _TERMINAL_STATUSES:
            return _result(build)
        self._guard_operation()
        if attempt >= self.settings.celery_max_poll_attempts:
            return _result(self.builds.fail(build_id, "Jenkins build polling exceeded the configured limit"))
        if build.status in {BuildStatus.QUEUED, BuildStatus.VALIDATING, BuildStatus.TRIGGERING}:
            return self._start_build(build_id, task_id)

        try:
            if build.status is BuildStatus.DEPLOYING:
                return self._resume_deployment(build)
            if not build.jenkins_queue_url:
                raise JenkinsError("Jenkins queue reference is missing")

            build_number = build.jenkins_build_number
            if build_number is None:
                queue = self._jenkins_adapter().get_queue_item(build.jenkins_queue_url)
                self._guard_operation()
                if queue.cancelled:
                    raise JenkinsError("Jenkins build was cancelled")
                if queue.build_number is None:
                    self.repository.update_build(build_id, {"last_polled_at": utc_now(), "updated_at": utc_now()})
                    self._schedule_poll(build_id, attempt + 1)
                    return _result(build)
                build_number = queue.build_number
                build = self.repository.update_build(
                    build_id,
                    {"jenkins_build_number": build_number, "last_polled_at": utc_now(), "updated_at": utc_now()},
                ) or build
                self.builds.record_event(
                    build.appid,
                    build_id,
                    "build.jenkins_started",
                    "Jenkins assigned a build number",
                    jenkins_build_number=build_number,
                )

            current = self._jenkins_adapter().get_build(build_number)
            self._guard_operation()
            self.repository.update_build(build_id, {"last_polled_at": utc_now(), "updated_at": utc_now()})
            if current.state is JenkinsBuildState.BUILDING or current.state is JenkinsBuildState.UNKNOWN:
                self._schedule_poll(build_id, attempt + 1)
                return _result(build)
            if current.state is not JenkinsBuildState.SUCCEEDED:
                raise JenkinsError(self._jenkins_failure_message(build, current.number, current.result))
            return self._complete_deployment(build, current.number)
        except Exception as exc:
            failed = self._fail(build, exc, "Build polling or deployment failed")
            return _result(failed)

    def sync_base_images(self, task_id: str | None) -> dict[str, int]:
        """Mirror every distinct pinned catalog image and persist every outcome."""

        sources = sorted({image for component in self.catalog.components.values() for image in component["images"]})
        results = {"total": len(sources), "synced": 0, "failed": 0}
        try:
            registry = self._registry_adapter()
        except Exception as exc:
            for source in sources:
                self._save_base_image_failure(source, exc, "Base image synchronization is not configured", task_id)
                results["failed"] += 1
            return results

        for source in sources:
            try:
                outcome = registry.sync(source)
            except Exception as exc:
                self._save_base_image_failure(source, exc, "Base image synchronization failed", task_id)
                results["failed"] += 1
                continue
            self.repository.save_base_image(outcome)
            self.builds.record_event(
                _SYSTEM_APPID,
                None,
                "base_image.synced",
                "Base image synchronized",
                source_image=source,
                harbor_reference=outcome.harbor_reference,
                digest=outcome.digest,
                task_id=task_id,
            )
            results["synced"] += 1
        return results

    def recover_builds(self, task_id: str | None) -> dict[str, int]:
        """Requeue non-terminal builds after a worker or broker interruption."""

        results = {"start_scheduled": 0, "poll_scheduled": 0}
        # A recovery tick must not add a second polling chain while the normal
        # chain is still active. Without this lease window, every recovery
        # tick schedules ``attempt=0`` again; the chains then race during
        # deployment and Docker Swarm can reject concurrent service updates.
        stale_after = max(
            float(self.settings.celery_recovery_interval_seconds) * 2,
            float(self.settings.deployment_readiness_timeout_seconds)
            + float(self.settings.celery_poll_interval_seconds) * 2,
            30.0,
        )
        for build in self.repository.list_active_builds():
            if not build.inputs_ready:
                age = (utc_now() - self._utc_aware_datetime(build.created_at)).total_seconds()
                if age >= max(3600, stale_after):
                    self.repository.ensure_build_resources(BuildResources(build_id=build.build_id, appid=build.appid, ownership_version=0))
                    with build_lease(self.repository, build.build_id, [CleanupStatus.NOT_STARTED]) as tracker:
                        current = self.builds.get_build(build.build_id)
                        if tracker is not None and not current.inputs_ready:
                            tracker.guard()
                            self.builds.fail(build.build_id, "Build input snapshot preparation was interrupted; create a new build or clean its private resources")
                continue
            if build.status in {BuildStatus.QUEUED, BuildStatus.VALIDATING, BuildStatus.TRIGGERING}:
                self._schedule_start(build.build_id)
                results["start_scheduled"] += 1
            elif build.status in {BuildStatus.BUILDING, BuildStatus.DEPLOYING}:
                if build.last_polled_at is not None:
                    last_polled_at = self._utc_aware_datetime(build.last_polled_at)
                    age = (utc_now() - last_polled_at).total_seconds()
                    if age < stale_after:
                        continue
                self._schedule_poll(build.build_id, 0)
                results["poll_scheduled"] += 1
        return results

    def cleanup_build(self, build_id: str) -> dict[str, str]:
        from orchestrator.build_resources import BuildCleanupRunner, BuildResourceService

        service = BuildResourceService(self.repository, self.builds.dispatcher, self.workspace, jenkins_factory=self._jenkins_adapter)
        return BuildCleanupRunner(self.settings, service).run(build_id)

    def recover_cleanups(self) -> dict[str, int]:
        from orchestrator.build_resources import BuildResourceService

        service = BuildResourceService(self.repository, self.builds.dispatcher, self.workspace, jenkins_factory=self._jenkins_adapter)
        return {"cleanup_scheduled": service.recover()}

    @staticmethod
    def _utc_aware_datetime(value: datetime) -> datetime:
        """Normalize repository timestamps before comparing them with UTC now."""

        if value.tzinfo is None or value.utcoffset() is None:
            # PyMongo returns naive datetimes unless tz_aware is enabled; the
            # repository contract stores those values as UTC.
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _application(self, build: BuildJob) -> UserAppRecord:
        app = self.repository.get_app(build.appid)
        if app is None:
            raise NotFoundError("Application for build was not found")
        manifest = self.repository.get_build_resources(build.build_id)
        if manifest is not None and manifest.ownership_version == 1:
            return app.model_copy(update={"repository_url": manifest.repository_url or "", "environment_ciphertext": manifest.environment_ciphertext})
        return app

    def _isolated(self, build: BuildJob) -> bool:
        manifest = self.repository.get_build_resources(build.build_id)
        return manifest is not None and manifest.ownership_version == 1

    def _docker_validation_adapter(self) -> DockerSwarmAdapter:
        return DockerSwarmAdapter.from_settings(self.settings)

    def _complete_deployment(self, build: BuildJob, build_number: int) -> dict[str, str]:
        artifact = self._jenkins_adapter().get_result_artifact(build_number)
        app = self._application(build)
        source = build.compose_snapshot if build.compose_snapshot is not None else app.compose
        self._validate_artifact_topology(source, artifact.compose)
        deployment_compose = self._workspace_compose(build, artifact.compose)
        self._validate_deployment_compose(deployment_compose)
        tracker = self._operation_tracker.get()
        tracker.guard()
        if self._isolated(build):
            if artifact.get("build_id") != build.build_id:
                raise BuildInputError("Jenkins artifact does not match the isolated build; upgrade the pipeline contract")
            if self.workspace.build_source(source) != "workspace" and any(service.get("build") is not None for service in source["services"].values()):
                actual_commit = artifact.get("source", {}).get("actual_commit")
                if not isinstance(actual_commit, str) or not re.fullmatch(r"[a-f0-9]{40}", actual_commit):
                    raise BuildInputError("Jenkins artifact is missing its immutable checkout commit")
                if build.git_commit_sha and build.git_commit_sha != actual_commit:
                    raise BuildInputError("Jenkins checkout commit does not match the frozen build input")
                tracker.guard()
                build = self.repository.update_build(build.build_id, {"git_commit_sha": actual_commit, "updated_at": utc_now()}) or build
            for name, service in deployment_compose["services"].items():
                if service["image"] != f"{self._image_repository(build.appid)}:{build.build_id}-{name}":
                    raise BuildInputError("Jenkins service image does not match its build-exclusive reference")
            for reference in artifact.images:
                if not reference.startswith(self._image_repository(build.appid) + ":" + build.build_id + "-"):
                    raise BuildInputError("Jenkins artifact contains a non-build-exclusive image reference")
                digest = artifact.get("image_digests", {}).get(reference)
                if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                    raise BuildInputError("Jenkins artifact is missing a verified build image digest")
            if set(artifact.images) != {service["image"] for service in deployment_compose["services"].values()}:
                raise BuildInputError("Jenkins artifact image inventory does not match deployment services")
        for reference in artifact.images:
            if self._isolated(build):
                tracker.record("registry_image", reference, target="harbor", state=ResourceState.PRESENT,
                               metadata={"build_id": build.build_id, "digest": artifact.get("image_digests", {}).get(reference)})
            self.repository.save_user_image(
                UserImage(
                    image_id=_image_id(build.build_id, reference),
                    appid=build.appid,
                    build_id=build.build_id,
                    reference=reference,
                )
            )
        if build.status is BuildStatus.BUILDING:
            build = self.builds.transition(build.build_id, BuildStatus.DEPLOYING, images=list(artifact.images))
            self.builds.record_event(
                build.appid,
                build.build_id,
                "build.images_registered",
                "Jenkins build images registered",
                image_count=len(artifact.images),
            )
        elif build.status is not BuildStatus.DEPLOYING:
            return _result(self.builds.get_build(build.build_id))

        adapter = self._docker_services_adapter()
        if self._isolated(build):
            deployment = adapter.deploy_build(build.appid, build.build_id, deployment_compose, tracker)
        else:
            deployment = adapter.deploy(build.appid, deployment_compose)
        self._probe_public_ports(deployment)
        tracker.guard()
        records = deployment.to_models(build.build_id)
        for record in records:
            tracker.guard()
            self.repository.save_service(record)
        tracker.guard()
        succeeded = self.builds.transition(
            build.build_id,
            BuildStatus.SUCCEEDED,
            service_ids=[record.service_id for record in records],
        )
        self.builds.record_event(
            succeeded.appid,
            succeeded.build_id,
            "build.deployed",
            "Docker Services deployment completed",
            service_count=len(records),
        )
        return _result(succeeded)

    def _probe_public_ports(self, deployment: Any) -> None:
        """Confirm configured published TCP ports accept connections.

        This is deliberately enabled only when the test/production settings
        advertise a public host. Internal-only Compose services remain valid,
        while a service that claims an external port cannot be marked
        successful before that port is reachable from the worker network.
        """

        raw_host = self.settings.public_host
        if not isinstance(raw_host, str) or not raw_host.strip():
            return
        host = raw_host.strip().rstrip("/")
        if "://" in host:
            parsed = urlsplit(host)
            host = parsed.hostname or ""
        else:
            host = host.strip("[]")
        if not host:
            raise DockerServiceError("Public host is invalid for deployment readiness checks")

        timeout = float(getattr(self.settings, "deployment_readiness_timeout_seconds", 60))
        interval = float(getattr(self.settings, "deployment_readiness_poll_interval_seconds", 1.0))
        for service in getattr(deployment, "services", ()):
            for port in getattr(service, "ports", ()):
                published = getattr(port, "published_port", None)
                protocol = getattr(port, "protocol", None)
                if published is None or protocol != "tcp":
                    continue
                deadline = time.monotonic() + timeout
                last_error: OSError | None = None
                while True:
                    try:
                        with socket.create_connection((host, int(published)), timeout=min(5.0, max(0.1, timeout))):
                            break
                    except OSError as exc:
                        last_error = exc
                    if time.monotonic() >= deadline:
                        detail = type(last_error).__name__ if last_error is not None else "connection failed"
                        raise DockerServiceError(
                            f"Published port {service.service_name}:{published} is not reachable ({detail})"
                        )
                    time.sleep(min(interval, max(0.01, deadline - time.monotonic())))

    def _resume_deployment(self, build: BuildJob) -> dict[str, str]:
        if build.jenkins_build_number is None:
            raise JenkinsError("Jenkins build number is missing for deployment recovery")
        return self._complete_deployment(build, build.jenkins_build_number)

    def _save_base_image_failure(self, source: str, exc: Exception, fallback: str, task_id: str | None) -> None:
        outcome = BaseImage(
            source_image=source,
            harbor_reference="",
            status="failed",
            error=_safe_error(exc, fallback),
            synced_at=utc_now(),
        )
        self.repository.save_base_image(outcome)
        self.builds.record_event(
            _SYSTEM_APPID,
            None,
            "base_image.failed",
            "Base image synchronization failed",
            EventLevel.ERROR,
            source_image=source,
            task_id=task_id,
        )

    def _schedule_poll(self, build_id: str, attempt: int) -> None:
        self._guard_operation()
        if self._poll_scheduler is not None:
            self._poll_scheduler(build_id, attempt, self.settings.celery_poll_interval_seconds)
            return
        from orchestrator.celery_app import create_celery_app

        create_celery_app(self.settings).send_task(
            "orchestrator.pipeline.poll_build",
            args=[build_id, attempt],
            queue=self.settings.celery_build_queue,
            countdown=self.settings.celery_poll_interval_seconds,
        )

    def _schedule_start(self, build_id: str) -> None:
        self._guard_operation()
        if self._start_scheduler is not None:
            self._start_scheduler(build_id)
            return
        from orchestrator.celery_app import create_celery_app

        create_celery_app(self.settings).send_task(
            "orchestrator.pipeline.start_build",
            args=[build_id],
            queue=self.settings.celery_build_queue,
        )

    def _image_repository(self, appid: str) -> str:
        if not self.settings.harbor_url:
            raise BuildInputError("Harbor registry URL is required for builds")
        raw = self.settings.harbor_url.strip()
        try:
            parsed = urlsplit(raw if "://" in raw else f"//{raw}")
            # Accessing ``port`` makes urlsplit reject malformed ports such as
            # ``harbor.example:bad`` instead of allowing them to reach Docker.
            parsed.port
        except ValueError as exc:
            raise BuildInputError("Harbor registry URL is invalid") from exc
        if (
            not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in raw
            or "#" in raw
            or (parsed.scheme and parsed.scheme not in {"http", "https"})
        ):
            raise BuildInputError("Harbor registry URL is invalid")
        if not is_docker_repository_component(self.settings.harbor_project):
            raise BuildInputError("Harbor project must be a lowercase Docker repository component")
        try:
            repository = f"{parsed.netloc}/{self.settings.harbor_project}/{external_namespace(appid)}"
            return validate_image_repository(repository)
        except NamingError as exc:
            raise BuildInputError(str(exc)) from exc

    def _workspace_compose(self, build: BuildJob, compose: Any) -> Any:
        self._validate_compose_structure(compose)
        if build.workspace_snapshot is None:
            return compose
        if self._isolated(build):
            return self.workspace.resolve_build_compose(build.appid, build.workspace_snapshot, compose)
        return self.workspace.resolve_compose(build.appid, build.workspace_snapshot, compose)

    def _workspace_archive_url(self, build: BuildJob, compose: Any) -> str:
        if self.workspace.build_source(compose) == "workspace" and not build.workspace_file_count:
            raise BuildInputError("Workspace build source is empty; upload files before creating the build")
        if not build.workspace_file_count and self.workspace.build_source(compose) != "workspace":
            return ""
        base = self.settings.workspace_download_base_url
        if not base or urlsplit(base).scheme not in ("http", "https"):
            raise BuildInputError("ORCHESTRATOR_WORKSPACE_DOWNLOAD_BASE_URL must be reachable from the Jenkins builder")
        if not build.workspace_snapshot:
            raise BuildInputError("Workspace build snapshot is missing; create a new build")
        return f"{base.rstrip('/')}/api/v1/workspace/{build.appid}/builds/{build.build_id}/archive"

    def _workspace_archive_token(self, build: BuildJob) -> str:
        return jwt.encode(
            {"scope": "workspace-download", "appid": build.appid, "build_id": build.build_id,
             "exp": datetime.now(timezone.utc) + timedelta(hours=24)},
            self.settings.jwt_secret, algorithm="HS256",
        )

    @staticmethod
    def _validate_source_compose(compose: Any) -> None:
        """Check delivery fields before contacting GitLab or Jenkins.

        Jenkins consumes ``build`` and produces the final service images.
        Validate the remaining runtime fields without mutating the saved
        source document or requiring those images to exist yet.
        """

        WorkerRuntime._validate_compose_structure(compose)
        runtime_compose = dict(compose)
        runtime_compose["services"] = {
            name: {key: value for key, value in service.items() if key != "build"}
            for name, service in compose["services"].items()
        }
        try:
            DockerSwarmAdapter.validate_compose(runtime_compose, require_images=False)
        except DockerServiceError as exc:
            raise BuildInputError(str(exc)) from exc

    @staticmethod
    def _validate_compose_structure(compose: Any) -> None:
        if not isinstance(compose, Mapping):
            raise BuildInputError("Build compose document must be a mapping")
        services = compose.get("services")
        if not isinstance(services, Mapping) or not services:
            raise BuildInputError("Build compose document must contain services")
        if not all(isinstance(name, str) and isinstance(service, Mapping) for name, service in services.items()):
            raise BuildInputError("Build compose services must be named mappings")

    @staticmethod
    def _validate_deployment_compose(compose: Any, *, source_compose: Any | None = None) -> None:
        WorkerRuntime._validate_compose_structure(compose)
        services = compose["services"]
        if not all(isinstance(service.get("image"), str) and service["image"].strip() for service in services.values()):
            raise BuildInputError("Jenkins deployment compose services must define images")
        try:
            DockerSwarmAdapter.validate_compose(compose)
            if source_compose is not None:
                WorkerRuntime._validate_artifact_topology(source_compose, compose)
        except DockerServiceError as exc:
            raise BuildInputError(str(exc)) from exc

    @staticmethod
    def _validate_artifact_topology(source_compose: Any, artifact_compose: Any) -> None:
        """Ensure Jenkins preserves service, port, and persistence contracts.

        Jenkins is allowed to replace images and normalize other build-time
        fields, but the final deployment contract must retain the user-facing
        service topology, every declared port mapping, and the original volume
        declarations. Losing a volume's explicit name/external metadata would
        otherwise silently switch an existing database to a new empty volume.
        """

        WorkerRuntime._validate_compose_structure(source_compose)
        WorkerRuntime._validate_compose_structure(artifact_compose)
        source_services = source_compose["services"]
        artifact_services = artifact_compose["services"]
        if set(source_services) != set(artifact_services):
            raise BuildInputError("Jenkins deployment artifact changed the Compose service topology")
        source_volumes = source_compose.get("volumes")
        artifact_volumes = artifact_compose.get("volumes")
        if ({} if source_volumes is None else source_volumes) != (
            {} if artifact_volumes is None else artifact_volumes
        ):
            raise BuildInputError(
                "Jenkins deployment artifact changed top-level volumes; "
                "the final artifact must preserve the source Compose volume declarations"
            )
        for name, source_service in source_services.items():
            source_ports = DockerSwarmAdapter._ports(source_service.get("ports"))
            artifact_ports = DockerSwarmAdapter._ports(artifact_services[name].get("ports"))
            if source_ports != artifact_ports:
                raise BuildInputError(
                    f"Jenkins deployment artifact changed ports for service {name}; "
                    "the final artifact must preserve the source Compose port mappings"
                )
            source_mounts = source_service.get("volumes")
            artifact_mounts = artifact_services[name].get("volumes")
            if ([] if source_mounts is None else source_mounts) != (
                [] if artifact_mounts is None else artifact_mounts
            ):
                raise BuildInputError(
                    f"Jenkins deployment artifact changed volumes for service {name}; "
                    "the final artifact must preserve the source Compose mount definitions"
                )

    def _jenkins_failure_message(self, build: BuildJob, number: int, result: str | None) -> str:
        message = f"Jenkins build finished with {result or 'unknown result'}；Jenkins #{number}"
        try:
            app = self._application(build)
            environment = self.builds.application_service.secret_box.decrypt_environment(app.environment_ciphertext)
            sensitive_values = list(environment.values())
            sensitive_values.extend(
                value for key, value in self.settings.model_dump().items()
                if isinstance(value, str) and re.search(r"password|secret|token|encryption_key", key, re.IGNORECASE)
            )
            compose = build.compose_snapshot if build.compose_snapshot is not None else app.compose
            for service in compose.get("services", {}).values():
                inputs = [service.get("environment", {})]
                if isinstance(service.get("build"), Mapping):
                    inputs.append(service["build"].get("args", {}))
                for values in inputs:
                    if isinstance(values, Mapping):
                        sensitive_values.extend(value for value in values.values() if isinstance(value, str))
                    elif isinstance(values, list):
                        sensitive_values.extend(value.split("=", 1)[1] for value in values if isinstance(value, str) and "=" in value)
            summary = self._jenkins_adapter().get_failure_summary(number, sensitive_values=sensitive_values)
        except Exception:
            summary = "错误详情暂不可用（无法读取或解析 Jenkins 控制台日志）；请查看该 Jenkins 构建的控制台"
        return f"{message}；{summary}"

    def _fail(self, build: BuildJob, exc: Exception, fallback: str) -> BuildJob:
        self._guard_operation()
        return self.builds.fail(build.build_id, _safe_error(exc, fallback))


def _image_id(build_id: str, reference: str) -> str:
    return hashlib.sha256(f"{build_id}:{reference}".encode()).hexdigest()[:32]


def _result(build: BuildJob) -> dict[str, str]:
    return {"build_id": build.build_id, "status": build.status.value}


def _safe_error(exc: Exception, fallback: str) -> str:
    """Only promote errors produced by adapters that deliberately redact secrets."""

    if isinstance(exc, (BuildInputError, DockerServiceError, GitLabError, JenkinsError, RegistrySyncError, TemplateError, ValueError)):
        message = str(exc).strip()
        if message:
            return message[:1500] if isinstance(exc, JenkinsError) else message[:500]
    return fallback


@lru_cache
def get_worker_runtime() -> WorkerRuntime:
    return WorkerRuntime(get_settings())
