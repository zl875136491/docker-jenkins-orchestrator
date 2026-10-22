from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta, timezone
from hashlib import sha256
import json
from threading import Event, Thread
from typing import Any
from uuid import uuid4
from urllib.parse import urlsplit

from orchestrator.models import (
    BuildResource, BuildResources, CleanupStatus, ResourceState, TERMINAL_BUILD_STATUSES, utc_now,
)
from orchestrator.naming import build_namespace
from orchestrator.naming import external_namespace
from orchestrator.repository import RepositoryError
from orchestrator.services import NotFoundError


class ResourceConflict(ValueError):
    pass


def resource_key(kind: str, target: str, name: str) -> str:
    return sha256(json.dumps([kind, target, name]).encode()).hexdigest()


class ResourceTracker:
    def __init__(self, repository: Any, build_id: str, owner: str | None = None) -> None:
        self.repository = repository
        self.build_id = build_id
        self.owner = owner

    def record(self, kind: str, name: str, *, target: str = "docker", native_id: str | None = None,
               state: ResourceState = ResourceState.PLANNED, managed: bool = True,
               metadata: dict[str, Any] | None = None, error: str | None = None) -> BuildResource:
        manifest = self.repository.get_build_resources(self.build_id)
        if manifest is None:
            raise RepositoryError("Build resources not found")
        key = resource_key(kind, target, name)
        existing = manifest.resources.get(key)
        if existing is not None:
            if native_id is not None and existing.native_id is not None and existing.native_id != native_id:
                raise ResourceConflict("Resource identity changed")
            if kind == "volume" and existing.metadata.get("creation_token") and metadata and "creation_token" in metadata and metadata["creation_token"] != existing.metadata["creation_token"]:
                raise ResourceConflict("Volume creation identity changed")
            if kind == "registry_image" and existing.metadata.get("digest") and metadata and metadata.get("digest") and metadata["digest"] != existing.metadata["digest"]:
                raise ResourceConflict("Build image digest changed")
            resource = existing.model_copy(update={
                "native_id": native_id or existing.native_id, "state": state, "managed": managed,
                "metadata": {**existing.metadata, **(metadata or {})}, "error": error, "updated_at": utc_now(),
            }, deep=True)
        else:
            resource = BuildResource(key=key, kind=kind, name=name, target=target, native_id=native_id,
                                     state=state, managed=managed, metadata=metadata or {}, error=error)
        self.repository.put_build_resource(self.build_id, resource, self.owner)
        return resource

    def update(self, resource: BuildResource, state: ResourceState, error: str | None = None) -> None:
        updated = resource.model_copy(update={"state": state, "error": error, "updated_at": utc_now()}, deep=True)
        self.repository.put_build_resource(self.build_id, updated, self.owner)

    def guard(self) -> None:
        manifest = self.repository.get_build_resources(self.build_id)
        if manifest is None:
            raise ResourceConflict("Build resource state is unavailable")
        if self.owner is not None:
            expiry = manifest.lease_expires_at
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if manifest.lease_owner != self.owner or expiry is None or expiry <= utc_now():
                raise ResourceConflict("Build resource lease lost")


@contextmanager
def build_lease(repository: Any, build_id: str, statuses: list[CleanupStatus], *, seconds: int = 120):
    owner = uuid4().hex
    manifest = repository.claim_build_lease(build_id, owner, utc_now() + timedelta(seconds=seconds), statuses)
    if manifest is None:
        yield None
        return
    stop = Event()

    def renew() -> None:
        while not stop.wait(max(1, seconds // 3)):
            try:
                if not repository.renew_build_lease(build_id, owner, utc_now() + timedelta(seconds=seconds)):
                    return
            except Exception:
                return

    thread = Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield ResourceTracker(repository, build_id, owner)
    finally:
        stop.set()
        thread.join(timeout=2)
        repository.release_build_lease(build_id, owner)


class BuildResourceService:
    def __init__(self, repository: Any, dispatcher: Any, workspace: Any, *, jenkins_factory: Any = None) -> None:
        self.repository = repository
        self.dispatcher = dispatcher
        self.workspace = workspace
        self.jenkins_factory = jenkins_factory

    def manifest(self, build_id: str) -> BuildResources:
        build = self.repository.get_build(build_id)
        if build is None:
            raise NotFoundError("Build not found")
        manifest = self.repository.get_build_resources(build_id)
        if manifest is not None:
            return manifest
        manifest = BuildResources(build_id=build_id, appid=build.appid, ownership_version=0)

        def protect(kind: str, name: str, *, target: str = "docker", native_id: str | None = None,
                    managed: bool = False, error: str | None = None) -> None:
            key = resource_key(kind, target, name)
            manifest.resources[key] = BuildResource(key=key, kind=kind, name=name, target=target,
                native_id=native_id, managed=managed, state=ResourceState.PRESENT if managed else ResourceState.BLOCKED, error=error)

        if build.workspace_snapshot:
            protect("workspace", str(self.workspace.snapshot_path(build.appid, build.workspace_snapshot)), target="workspace", managed=True)
        for service in self.repository.list_services(build.appid):
            if service.build_id == build_id:
                protect("service", service.service_name, native_id=service.service_id,
                        error="Legacy APP-scoped service is not proven build-exclusive")
        for image in self.repository.list_user_images(build.appid):
            if image.build_id == build_id:
                protect("registry_image", image.reference, target="harbor", error="Legacy image ownership requires explicit verification")
        source = build.compose_snapshot or {}
        for name in source.get("volumes", {}):
            protect("volume", name, error="Legacy APP-scoped volume is protected")
        if source.get("services"):
            protect("network", "legacy-app-network", error="Legacy APP-scoped network is protected")
        return self.repository.ensure_build_resources(manifest)

    def assert_quiescent(self, build_id: str) -> None:
        build = self.repository.get_build(build_id)
        if build is None:
            raise NotFoundError("Build not found")
        if build.status not in TERMINAL_BUILD_STATUSES:
            raise ResourceConflict("Build is still queued, building or deploying")
        manifest = self.repository.get_build_resources(build_id)
        if manifest and manifest.jenkins_trigger_pending and not build.jenkins_queue_url:
            raise ResourceConflict("Jenkins trigger outcome is unknown; external execution must be verified")
        if build.jenkins_queue_url or build.jenkins_build_number is not None:
            if self.jenkins_factory is None:
                raise ResourceConflict("Jenkins execution state cannot be verified")
            jenkins = self.jenkins_factory()
            if build.jenkins_build_number is not None:
                execution = jenkins.get_build(build.jenkins_build_number)
                if not execution.terminal:
                    raise ResourceConflict("Jenkins execution has not finished")
            else:
                queued = jenkins.get_queue_item(build.jenkins_queue_url)
                if queued.build_number is not None:
                    if not jenkins.get_build(queued.build_number).terminal:
                        raise ResourceConflict("Jenkins execution has not finished")
                elif not queued.cancelled:
                    raise ResourceConflict("Jenkins queue item has not finished")

    def request_cleanup(self, build_id: str, *, dispatch: bool = True) -> tuple[BuildResources, int]:
        manifest = self.manifest(build_id)
        if manifest.cleanup_status == CleanupStatus.SUCCEEDED:
            return manifest, 200
        if manifest.cleanup_status in {CleanupStatus.QUEUED, CleanupStatus.RUNNING}:
            return manifest, 202
        self.assert_quiescent(build_id)
        with build_lease(self.repository, build_id, [CleanupStatus.NOT_STARTED, CleanupStatus.PARTIAL_FAILED]) as tracker:
            if tracker is None:
                current = self.manifest(build_id)
                if current.cleanup_status in {CleanupStatus.QUEUED, CleanupStatus.RUNNING, CleanupStatus.SUCCEEDED}:
                    return current, 200 if current.cleanup_status == CleanupStatus.SUCCEEDED else 202
                raise ResourceConflict("Build resource operation is still running")
            current = self.manifest(build_id)
            queued = self.repository.update_build_resources(build_id, {
                "cleanup_status": CleanupStatus.QUEUED, "cleanup_generation": current.cleanup_generation + 1,
                "cleanup_requested_at": utc_now(), "finished_at": None, "last_error": None,
            }, {"lease_owner": tracker.owner, "cleanup_status": current.cleanup_status})
            if queued is None:
                raise ResourceConflict("Concurrent cleanup state change")
        if not dispatch:
            # Quality-gate teardown runs immediately in the current worker,
            # after releasing its deployment lease. It must not depend on a
            # healthy broker to reclaim resources from a failed delivery.
            return self.manifest(build_id), 202
        try:
            self.dispatcher.dispatch_build_cleanup(build_id)
        except Exception as exc:
            self.repository.update_build_resources(build_id, {
                "cleanup_status": CleanupStatus.PARTIAL_FAILED, "last_error": "Unable to enqueue cleanup task",
            }, {"cleanup_status": CleanupStatus.QUEUED, "cleanup_generation": queued.cleanup_generation})
            raise RepositoryError("Unable to enqueue cleanup task") from exc
        return self.manifest(build_id), 202

    def recover(self) -> int:
        scheduled = 0
        for manifest in self.repository.list_cleanup_resources():
            expiry = manifest.lease_expires_at
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if manifest.lease_owner and expiry and expiry > utc_now():
                continue
            self.dispatcher.dispatch_build_cleanup(manifest.build_id)
            scheduled += 1
        return scheduled


class BuildCleanupRunner:
    def __init__(self, settings: Any, service: BuildResourceService, *, docker_factory: Any = None, harbor_factory: Any = None) -> None:
        self.settings = settings
        self.service = service
        self.repository = service.repository
        self.workspace = service.workspace
        self.docker_factory = docker_factory
        self.harbor_factory = harbor_factory

    def expected_repository(self, appid: str) -> str:
        raw = self.settings.harbor_url or ""
        parsed = urlsplit(raw if "://" in raw else "//" + raw)
        return f"{parsed.netloc}/{self.settings.harbor_project}/{external_namespace(appid)}"

    def collect_builder_resources(self, manifest: BuildResources, tracker: ResourceTracker) -> None:
        build = self.repository.get_build(manifest.build_id)
        if manifest.ownership_version != 1 or not (build.jenkins_queue_url or build.jenkins_build_number is not None):
            return
        jenkins = self.service.jenkins_factory()
        number = build.jenkins_build_number
        if number is None:
            number = jenkins.get_queue_item(build.jenkins_queue_url).build_number
        if number is None:
            return
        report = jenkins.get_resource_artifact(number)
        if report.get("build_id") != build.build_id:
            raise ResourceConflict("Jenkins resource artifact has a different build owner")
        if report.get("verification_complete") is not True:
            raise ResourceConflict("Jenkins builder resource discovery is incomplete")
        if report.get("workspace_removed") is not True:
            raise ResourceConflict("Jenkins build-private workspace cleanup has not been verified")
        target = report.get("builder_target")
        expected = self.expected_repository(build.appid)
        images = report.get("images")
        if not isinstance(report.get("plan_created"), bool):
            raise ResourceConflict("Jenkins builder resource plan verification is missing")
        if not isinstance(images, list) or any(not isinstance(item, dict) or not isinstance(item.get("local_removed"), bool) for item in images):
            raise ResourceConflict("Jenkins resource artifact image inventory is invalid")
        references = {item.get("reference") for item in images if isinstance(item.get("reference"), str)}
        if report.get("plan_created") is True:
            planned = {f"{expected}:{build.build_id}-{name}" for name in (build.compose_snapshot or {}).get("services", {})}
            if references != planned or len(images) != len(planned):
                raise ResourceConflict("Jenkins builder resource inventory is incomplete")
        elif images:
            raise ResourceConflict("Jenkins builder resource plan is unavailable")
        for item in images:
            reference = item.get("reference", "")
            if not isinstance(reference, str) or not reference.startswith(expected + ":" + build.build_id + "-"):
                raise ResourceConflict("Jenkins resource artifact contains a non-build image reference")
            current = self.repository.get_build_resources(build.build_id)
            registry_item = current.resources.get(resource_key("registry_image", "harbor", reference))
            if registry_item is None or registry_item.state != ResourceState.REMOVED:
                tracker.record("registry_image", reference, target="harbor", state=ResourceState.PRESENT)
            if not item.get("local_removed"):
                if not isinstance(target, str) or not target.startswith("docker:"):
                    raise ResourceConflict("Jenkins builder execution target is missing")
                local_item = current.resources.get(resource_key("local_image", target, reference))
                if local_item is None or local_item.state != ResourceState.REMOVED:
                    tracker.record("local_image", reference, target=target, native_id=item.get("native_id"), state=ResourceState.PRESENT)

    def run(self, build_id: str) -> dict[str, str]:
        manifest = self.service.manifest(build_id)
        if manifest.cleanup_status not in {CleanupStatus.QUEUED, CleanupStatus.RUNNING}:
            return {"build_id": build_id, "cleanup_status": manifest.cleanup_status.value}
        with build_lease(self.repository, build_id, [CleanupStatus.QUEUED, CleanupStatus.RUNNING]) as tracker:
            if tracker is None:
                return {"build_id": build_id, "cleanup_status": self.service.manifest(build_id).cleanup_status.value}
            self.repository.update_build_resources(build_id, {
                "cleanup_status": CleanupStatus.RUNNING, "started_at": manifest.started_at or utc_now(), "last_error": None,
            }, {"lease_owner": tracker.owner})
            errors = []
            docker = None
            registry_states = []
            try:
                self.service.assert_quiescent(build_id)
            except Exception as exc:
                self.repository.update_build_resources(build_id, {
                    "cleanup_status": CleanupStatus.PARTIAL_FAILED, "finished_at": utc_now(), "last_error": self.safe_error(exc),
                }, {"lease_owner": tracker.owner})
                return {"build_id": build_id, "cleanup_status": CleanupStatus.PARTIAL_FAILED.value}
            try:
                self.collect_builder_resources(manifest, tracker)
            except Exception as exc:
                errors.append(self.safe_error(exc))
                tracker.record("builder_cache", "jenkins-resource-verification", target="jenkins", state=ResourceState.ERROR,
                               error=self.safe_error(exc))
            else:
                prior = self.repository.get_build_resources(build_id)
                for resource in prior.resources.values():
                    if resource.kind == "builder_cache" and resource.name == "jenkins-resource-verification":
                        tracker.update(resource, ResourceState.REMOVED)
            discovery_complete = manifest.ownership_version == 0
            try:
                from orchestrator.docker_cleanup import DockerBuildCleaner

                if manifest.ownership_version == 1:
                    factory = self.docker_factory or (lambda: DockerBuildCleaner(self.settings, self.repository))
                    docker = factory()
                    docker.discover(self.repository.get_build_resources(build_id), tracker)
                    discovery_complete = True
            except Exception as exc:
                errors.append(self.safe_error(exc))
            order = ["service", "container", "volume", "workspace", "runtime_directory", "network", "port", "local_image", "registry_image"]
            for kind in order:
                if kind == "container" and docker is not None and discovery_complete:
                    try:
                        tracker.guard()
                        docker.discover(self.repository.get_build_resources(build_id), tracker)
                        docker.wait_containers_stopped(manifest, tracker)
                    except Exception as exc:
                        errors.append(self.safe_error(exc))
                        discovery_complete = False
                current = self.repository.get_build_resources(build_id)
                for resource in list(current.resources.values()):
                    if resource.kind != kind or resource.state == ResourceState.REMOVED:
                        continue
                    tracker.guard()
                    if not resource.managed:
                        tracker.update(resource, ResourceState.BLOCKED, resource.error or "Resource ownership is not exclusive")
                        continue
                    try:
                        latest = self.repository.get_build_resources(build_id)
                        if kind not in {"service", "container"}:
                            outstanding = [item for item in latest.resources.values() if item.kind in {"service", "container"} and item.state != ResourceState.REMOVED]
                            if outstanding or not discovery_complete:
                                raise ResourceConflict("Build runtime resources have not been fully removed")
                        if kind in {"workspace", "runtime_directory"}:
                            if kind != "workspace" or resource.target != "workspace":
                                raise ResourceConflict("Unregistered directory cleanup target")
                            if docker is not None:
                                host_path = self.workspace.host_root / self.workspace.snapshot_path(manifest.appid, build_id).relative_to(self.workspace.root)
                                docker.assert_directory_unused(str(host_path), tracker)
                            tracker.guard()
                            self.workspace.delete_build_snapshot(manifest.appid, build_id, resource.name)
                        elif kind == "registry_image":
                            from orchestrator.harbor_cleanup import HarborImageCleaner

                            cleaner = self.harbor_factory() if self.harbor_factory else HarborImageCleaner(self.settings, guard=tracker.guard)
                            storage_state = cleaner.remove_build_tag(resource.name, self.expected_repository(manifest.appid), build_id, resource.metadata.get("digest"))
                            registry_states.append(storage_state)
                            resource = resource.model_copy(update={"metadata": {**resource.metadata, "registry_storage_status": storage_state}})
                        else:
                            if docker is None:
                                raise ResourceConflict("Docker cleanup target is unavailable")
                            docker.remove(manifest, resource, tracker)
                        tracker.update(resource, ResourceState.REMOVED)
                    except Exception as exc:
                        tracker.update(resource, ResourceState.ERROR, self.safe_error(exc))
            current = self.repository.get_build_resources(build_id)
            if docker is not None:
                try:
                    docker.discover(current, tracker)
                    current = self.repository.get_build_resources(build_id)
                    docker.verify_empty(current, tracker)
                except Exception as exc:
                    errors.append(self.safe_error(exc))
            current = self.repository.get_build_resources(build_id)
            remaining = [item for item in current.resources.values() if item.state != ResourceState.REMOVED]
            registry_states.extend(item.metadata["registry_storage_status"] for item in current.resources.values()
                                   if item.kind == "registry_image" and item.state == ResourceState.REMOVED and item.metadata.get("registry_storage_status"))
            status = CleanupStatus.PARTIAL_FAILED if remaining or errors else CleanupStatus.SUCCEEDED
            storage_status = "shared_retained" if "shared_retained" in registry_states else "waiting_gc" if registry_states else current.registry_storage_status
            last_error = "; ".join(errors) if errors else "Some build resources could not be removed" if remaining else None
            tracker.guard()
            updated = self.repository.update_build_resources(build_id, {
                "cleanup_status": status, "finished_at": utc_now(), "last_error": last_error,
                "registry_storage_status": storage_status,
            }, {"lease_owner": tracker.owner, "cleanup_status": CleanupStatus.RUNNING})
            if updated is None:
                raise ResourceConflict("Build cleanup lease lost before saving result")
            for service_record in self.repository.list_services(manifest.appid):
                if service_record.build_id != build_id:
                    continue
                associated = [item for item in current.resources.values() if item.kind == "service" and item.native_id == service_record.service_id]
                if associated and all(item.state == ResourceState.REMOVED for item in associated):
                    tracker.guard()
                    self.repository.save_service(service_record.model_copy(update={"status": "cleaned", "updated_at": utc_now()}))
            return {"build_id": build_id, "cleanup_status": status.value}

    @staticmethod
    def safe_error(exc: Exception) -> str:
        from orchestrator.adapters import AdapterError
        from orchestrator.workspaces import WorkspaceError

        if isinstance(exc, (ResourceConflict, AdapterError, WorkspaceError, RepositoryError)):
            return str(exc)
        return "Cleanup operation failed; inspect the protected resource or execution target"
