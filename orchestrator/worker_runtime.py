"""Durable Celery worker orchestration for builds and base images.

The runtime keeps external clients injectable so the full lifecycle can be
tested without contacting GitLab, Jenkins, Harbor, or a Docker daemon.  It
only exchanges identifiers through Celery; all meaningful state is persisted
through the configured repository.
"""

from __future__ import annotations

import hashlib
import socket
import time
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from orchestrator.config import Settings, get_settings
from orchestrator.docker_services import DockerServiceError, DockerSwarmAdapter
from orchestrator.gitlab import GitLabError, GitLabHttpAdapter
from orchestrator.jenkins import JenkinsBuildRequest, JenkinsBuildState, JenkinsError, JenkinsHttpAdapter
from orchestrator.models import BaseImage, BuildJob, BuildStatus, EventLevel, UserAppRecord, UserImage, utc_now
from orchestrator.registry import DockerRegistrySynchronizer, RegistrySyncError
from orchestrator.services import BuildInputError, BuildService, NotFoundError
from orchestrator.templates import TemplateCatalog, TemplateError


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
        """Validate input, trigger Jenkins once, and schedule a durable poll."""

        build = self.builds.get_build(build_id)
        if build.status in _TERMINAL_STATUSES:
            return _result(build)
        app = self._application(build)

        try:
            if build.celery_task_id is None and task_id:
                build = self.repository.update_build(build_id, {"celery_task_id": task_id, "updated_at": utc_now()}) or build
            if build.status is BuildStatus.QUEUED:
                build = self.builds.transition(build_id, BuildStatus.VALIDATING)

            if build.status is BuildStatus.VALIDATING:
                self._validate_source_compose(app.compose)
                self.builds.record_event(app.appid, build_id, "build.compose_validated", "Build compose input validated")
                git_ref = build.git_ref
                gitlab = self._gitlab_adapter()
                if gitlab is not None:
                    resolved = gitlab.validate_repository_ref(app.repository_url, build.git_ref)
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
                build = self.builds.transition(build_id, BuildStatus.TRIGGERING)
            else:
                git_ref = build.git_commit_sha or build.git_ref

            if build.status is BuildStatus.TRIGGERING and not build.jenkins_queue_url:
                queued = self._jenkins_adapter().trigger_build(
                    JenkinsBuildRequest(
                        appid=app.appid,
                        repository_url=app.repository_url,
                        git_ref=git_ref,
                        environment=self.builds.application_service.get_environment(app.appid),
                        compose=app.compose or {},
                        image_repository=self._image_repository(app.appid),
                    )
                )
                build = self.repository.update_build(
                    build_id,
                    {"jenkins_queue_url": queued.queue_url, "updated_at": utc_now()},
                ) or build
                self.builds.record_event(
                    app.appid,
                    build_id,
                    "build.jenkins_triggered",
                    "Jenkins build triggered",
                    queue_id=queued.queue_id,
                )

            if build.status is BuildStatus.TRIGGERING:
                build = self.builds.transition(build_id, BuildStatus.BUILDING)
            if build.status in {BuildStatus.BUILDING, BuildStatus.DEPLOYING}:
                self._schedule_poll(build_id, 0)
            return _result(build)
        except Exception as exc:
            failed = self._fail(build, exc, "Build validation or Jenkins trigger failed")
            return _result(failed)

    def poll_build(self, build_id: str, attempt: int, task_id: str | None) -> dict[str, str]:
        """Poll Jenkins or resume deployment after a worker retry/restart."""

        build = self.builds.get_build(build_id)
        if build.status in _TERMINAL_STATUSES:
            return _result(build)
        if attempt >= self.settings.celery_max_poll_attempts:
            return _result(self.builds.fail(build_id, "Jenkins build polling exceeded the configured limit"))
        if build.status in {BuildStatus.QUEUED, BuildStatus.VALIDATING, BuildStatus.TRIGGERING}:
            return self.start_build(build_id, task_id)

        try:
            if build.status is BuildStatus.DEPLOYING:
                return self._resume_deployment(build)
            if not build.jenkins_queue_url:
                raise JenkinsError("Jenkins queue reference is missing")

            build_number = build.jenkins_build_number
            if build_number is None:
                queue = self._jenkins_adapter().get_queue_item(build.jenkins_queue_url)
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
            self.repository.update_build(build_id, {"last_polled_at": utc_now(), "updated_at": utc_now()})
            if current.state is JenkinsBuildState.BUILDING or current.state is JenkinsBuildState.UNKNOWN:
                self._schedule_poll(build_id, attempt + 1)
                return _result(build)
            if current.state is not JenkinsBuildState.SUCCEEDED:
                raise JenkinsError(f"Jenkins build finished with {current.result or 'unknown result'}")
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
        for build in self.repository.list_active_builds():
            if build.status in {BuildStatus.QUEUED, BuildStatus.VALIDATING, BuildStatus.TRIGGERING}:
                self._schedule_start(build.build_id)
                results["start_scheduled"] += 1
            elif build.status in {BuildStatus.BUILDING, BuildStatus.DEPLOYING}:
                self._schedule_poll(build.build_id, 0)
                results["poll_scheduled"] += 1
        return results

    def _application(self, build: BuildJob) -> UserAppRecord:
        app = self.repository.get_app(build.appid)
        if app is None:
            raise NotFoundError("Application for build was not found")
        return app

    def _complete_deployment(self, build: BuildJob, build_number: int) -> dict[str, str]:
        artifact = self._jenkins_adapter().get_result_artifact(build_number)
        app = self._application(build)
        self._validate_deployment_compose(artifact.compose, source_compose=app.compose)
        for reference in artifact.images:
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

        deployment = self._docker_services_adapter().deploy(build.appid, artifact.compose)
        self._probe_public_ports(deployment)
        records = deployment.to_models(build.build_id)
        for record in records:
            self.repository.save_service(record)
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
        raw = self.settings.harbor_url.strip().rstrip("/")
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        if not parsed.netloc or parsed.username or parsed.password or parsed.path not in {"", "/"}:
            raise BuildInputError("Harbor registry URL is invalid")
        return f"{parsed.netloc}/{self.settings.harbor_project}/{appid}"

    @staticmethod
    def _validate_source_compose(compose: Any) -> None:
        if not isinstance(compose, Mapping):
            raise BuildInputError("Build compose document must be a mapping")
        services = compose.get("services")
        if not isinstance(services, Mapping) or not services:
            raise BuildInputError("Build compose document must contain services")
        if not all(isinstance(name, str) and isinstance(service, Mapping) for name, service in services.items()):
            raise BuildInputError("Build compose services must be named mappings")

    @staticmethod
    def _validate_deployment_compose(compose: Any, *, source_compose: Any | None = None) -> None:
        WorkerRuntime._validate_source_compose(compose)
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
        """Ensure Jenkins cannot silently remove a service or published port.

        Jenkins is allowed to replace images and normalize other build-time
        fields, but the final deployment contract must retain the user-facing
        service topology and every declared port mapping. A missing mapping
        otherwise looks like a successful deployment while making the app
        unreachable from outside Swarm.
        """

        WorkerRuntime._validate_source_compose(source_compose)
        source_services = source_compose["services"]
        artifact_services = artifact_compose["services"]
        if set(source_services) != set(artifact_services):
            raise BuildInputError("Jenkins deployment artifact changed the Compose service topology")
        for name, source_service in source_services.items():
            source_ports = DockerSwarmAdapter._ports(source_service.get("ports"))
            artifact_ports = DockerSwarmAdapter._ports(artifact_services[name].get("ports"))
            if source_ports != artifact_ports:
                raise BuildInputError(
                    f"Jenkins deployment artifact changed ports for service {name}; "
                    "the final artifact must preserve the source Compose port mappings"
                )

    def _fail(self, build: BuildJob, exc: Exception, fallback: str) -> BuildJob:
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
            return message[:500]
    return fallback


@lru_cache
def get_worker_runtime() -> WorkerRuntime:
    return WorkerRuntime(get_settings())
