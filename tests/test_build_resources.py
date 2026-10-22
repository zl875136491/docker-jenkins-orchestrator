from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from main import create_app
from orchestrator.build_resources import BuildCleanupRunner, ResourceConflict, ResourceTracker, build_lease
from orchestrator.config import Settings
from orchestrator.container import create_container
from orchestrator.docker_cleanup import DockerBuildCleaner
from orchestrator.docker_services import DockerServiceError, DockerSwarmAdapter
from orchestrator.dynamic_checks import DynamicCheckService
from orchestrator.models import BuildCreate, BuildJob, BuildStatus, CleanupStatus, DeploymentService, ResourceState, UserAppCreate, UserAppUpdate, utc_now
from orchestrator.naming import build_namespace
from orchestrator.jenkins import JenkinsBuild
from orchestrator.repository import RepositoryError
from tests.route_check_fixtures import prepare_static_pass


class NotFound(Exception):
    status_code = 404


class FakeObject:
    def __init__(self, collection, name, attrs):
        self.collection = collection
        self.name = name
        self.id = name if collection.kind == "volume" else collection.kind + "-" + name
        self.attrs = attrs
        self.status = "running"
        self.tags = []

    def remove(self, **kwargs):
        if self.collection.fail_remove:
            raise RuntimeError("unavailable")
        if self.collection.kind == "service":
            for container in self.collection.docker.containers.items.values():
                if container.attrs["Config"]["Labels"].get("com.docker.swarm.service.id") == self.id:
                    container.status = "exited"
        self.collection.items.pop(self.name, None)

    def stop(self, **kwargs):
        self.status = "exited"

    def tasks(self):
        return [{"DesiredState": "running", "NodeID": "node", "Status": {
            "State": "running", "ContainerStatus": {"ContainerID": container.id},
        }} for container in self.collection.docker.containers.items.values()
            if container.attrs["Config"]["Labels"].get("com.docker.swarm.service.id") == self.id]

    def update(self, **kwargs):
        raise AssertionError("An independent new build must not update an existing build service")


class FakeCollection:
    def __init__(self, docker, kind):
        self.docker = docker
        self.kind = kind
        self.items = {}
        self.fail_remove = False
        self.create_calls = []

    def get(self, identifier):
        for obj in self.items.values():
            if identifier in {obj.name, obj.id} or identifier in obj.tags:
                return obj
        raise NotFound(identifier)

    def list(self, all=False, filters=None):
        items = list(self.items.values())
        filters = filters or {}
        labels = filters.get("label", [])
        if isinstance(labels, str):
            labels = [labels]
        for label in labels:
            key, value = label.split("=", 1)
            items = [obj for obj in items if (obj.attrs.get("Spec", {}).get("Labels") or obj.attrs.get("Labels") or obj.attrs.get("Config", {}).get("Labels") or {}).get(key) == value]
        if "volume" in filters:
            items = [obj for obj in items if any(mount.get("Source") == filters["volume"] for mount in obj.attrs.get("Mounts", []))]
        if "ancestor" in filters:
            items = [obj for obj in items if obj.attrs.get("Image") == filters["ancestor"]]
        if "network" in filters:
            items = [obj for obj in items if any(network.get("Target") == filters["network"] for network in obj.attrs.get("Spec", {}).get("TaskTemplate", {}).get("Networks", []))]
        return items

    def create(self, image=None, *, name=None, **kwargs):
        self.create_calls.append((image, {"name": name, **kwargs}))
        if self.kind == "network":
            name = image if name is None else name
        labels = kwargs.get("labels", {})
        attrs = {"Labels": labels, "Name": name}
        if self.kind == "volume":
            attrs["CreatedAt"] = uuid4().hex
        if self.kind == "service":
            ports = (kwargs.get("endpoint_spec") or {}).get("Ports", [])
            attrs = {"Spec": {"Name": name, "Labels": labels, "Mode": {"Replicated": {"Replicas": 1}},
                     "TaskTemplate": {"Networks": kwargs.get("networks", []), "ContainerSpec": {"Image": image}}}, "Endpoint": {"Ports": ports}}
        obj = FakeObject(self, name, attrs)
        self.items[name] = obj
        if self.kind == "service":
            image_obj = self.docker.images.ensure(image)
            container_labels = {**kwargs.get("container_labels", {}), "com.docker.swarm.service.id": obj.id}
            mounts = []
            for raw in kwargs.get("mounts", []):
                if isinstance(raw, str):
                    source, destination = raw.split(":")[:2]
                    mounts.append({"Source": source, "Destination": destination, "Type": "bind" if source.startswith("/") else "volume"})
            container = FakeObject(self.docker.containers, name + "-task", {
                "Config": {"Labels": container_labels, "Image": image}, "Image": image_obj.id, "Mounts": mounts,
            })
            self.docker.containers.items[container.name] = container
        return obj


class FakeImages(FakeCollection):
    def ensure(self, reference):
        try:
            return self.get(reference)
        except NotFound:
            if self.items:
                image = next(iter(self.items.values()))
            else:
                image = FakeObject(self, "shared-content", {})
                self.items[image.name] = image
            image.tags.append(reference)
            return image

    def remove(self, reference, **kwargs):
        assert kwargs.get("force") is False
        image = self.get(reference)
        image.tags.remove(reference)
        if not image.tags:
            self.items.pop(image.name)


class FakeDocker:
    def __init__(self):
        self.services = FakeCollection(self, "service")
        self.networks = FakeCollection(self, "network")
        self.volumes = FakeCollection(self, "volume")
        self.containers = FakeCollection(self, "container")
        self.images = FakeImages(self, "image")

    def info(self):
        return {"ID": "engine", "Swarm": {"NodeID": "node"}}


class FakeHarbor:
    def __init__(self):
        self.removed = []

    def remove_build_tag(self, reference, expected, build_id, digest=None):
        assert reference.startswith(expected + ":" + build_id + "-")
        self.removed.append(reference)
        return "waiting_gc"


@pytest.fixture
def harness(tmp_path):
    settings = Settings(environment="test", harbor_url="https://harbor.example", workspace_root=tmp_path / "workspace",
                        workspace_host_root=tmp_path / "workspace", public_host="127.0.0.1", cleanup_timeout_seconds=1)
    container = create_container(settings)
    compose = {"services": {"web": {"image": "base:test", "ports": ["18080:8080"], "volumes": ["data:/data"]}}, "volumes": {"data": {}}}
    container.applications.create_app(UserAppCreate(appid="demo", name="Demo", repository_url="https://git.example/demo.git", compose=compose, environment={"VALUE": "original"}))
    prepare_static_pass(container, "demo")
    docker = FakeDocker()
    harbor = FakeHarbor()
    adapter = DockerSwarmAdapter.from_settings(settings, docker_client=docker)
    cleaner = DockerBuildCleaner(settings, container.repository, client=docker)
    runner = BuildCleanupRunner(settings, container.build_resources, docker_factory=lambda: cleaner, harbor_factory=lambda: harbor)
    yield SimpleNamespace(settings=settings, container=container, docker=docker, adapter=adapter, runner=runner, harbor=harbor, compose=compose)
    container.close()


def deploy(harness):
    repository = harness.container.repository
    build = harness.container.builds.queue_build("demo", BuildCreate())
    compose = deepcopy(harness.compose)
    reference = f"harbor.example/apps/demo:{build.build_id}-web"
    compose["services"]["web"]["image"] = reference
    with build_lease(repository, build.build_id, [CleanupStatus.NOT_STARTED]) as tracker:
        tracker.record("registry_image", reference, target="harbor", state=ResourceState.PRESENT)
        result = harness.adapter.deploy_build("demo", build.build_id, compose, tracker)
        for service in result.to_models(build.build_id):
            repository.save_service(service)
        repository.update_build(build.build_id, {"status": BuildStatus.SUCCEEDED})
    return build, result


def test_two_builds_have_independent_services_networks_volumes_ports_and_image_references(harness):
    first, first_deployment = deploy(harness)
    second, second_deployment = deploy(harness)
    assert first_deployment.network_name != second_deployment.network_name
    assert first_deployment.services[0].service_id != second_deployment.services[0].service_id
    assert first_deployment.services[0].ports[0].published_port != second_deployment.services[0].ports[0].published_port
    assert len(harness.docker.volumes.items) == 2
    assert len(harness.docker.containers.items) == 2
    assert len(next(iter(harness.docker.images.items.values())).tags) == 2
    assert build_namespace("demo", first.build_id) != build_namespace("demo", second.build_id)


def test_cleanup_second_build_preserves_first_and_shared_image_layers(harness):
    first, first_deployment = deploy(harness)
    second, second_deployment = deploy(harness)
    first_manifest = harness.container.repository.get_build_resources(first.build_id)
    queued, status = harness.container.build_resources.request_cleanup(second.build_id)
    assert status == 202 and queued.cleanup_status == CleanupStatus.QUEUED
    assert harness.runner.run(second.build_id)["cleanup_status"] == "succeeded"
    assert harness.docker.services.get(first_deployment.services[0].service_id)
    with pytest.raises(NotFound):
        harness.docker.services.get(second_deployment.services[0].service_id)
    assert len(harness.docker.volumes.items) == 1
    assert len(harness.docker.networks.items) == 1
    assert len(harness.docker.containers.items) == 1
    image = next(iter(harness.docker.images.items.values()))
    assert image.tags == [first_deployment.services[0].image]
    assert harness.container.repository.get_build_resources(first.build_id) == first_manifest
    assert not harness.container.workspace.snapshot_path("demo", second.build_id).exists()
    assert harness.container.workspace.snapshot_path("demo", first.build_id).exists()
    assert harness.container.applications.get_app("demo")
    assert harness.container.repository.get_build(second.build_id).status == BuildStatus.SUCCEEDED
    assert harness.container.build_resources.request_cleanup(second.build_id)[1] == 200


def test_dynamic_failure_teardown_is_build_scoped_and_queryable(harness, monkeypatch):
    first, first_deployment = deploy(harness)
    second, second_deployment = deploy(harness)
    container = harness.container
    repository = container.repository
    repository.update_build(first.build_id, {
        "service_ids": [service.service_id for service in first_deployment.services],
        "dynamic_status": "succeeded", "dynamic_check_passed": True,
    })
    repository.update_build(second.build_id, {
        "status": BuildStatus.DEPLOYING,
        "service_ids": [service.service_id for service in second_deployment.services],
    })
    first_build = repository.get_build(first.build_id)
    first_manifest = repository.get_build_resources(first.build_id)
    probe_calls, debug_calls, log_calls, cleanup_calls = [], [], [], []

    def failed_probe(payload, *, require_route, endpoints):
        probe_calls.append((deepcopy(payload), require_route, list(endpoints)))
        assert require_route == "/health"
        assert [(endpoint.host, endpoint.port) for endpoint in endpoints] == [
            ("127.0.0.1", second_deployment.services[0].ports[0].published_port),
        ]
        assert endpoints[0].port != first_deployment.services[0].ports[0].published_port
        return {"passed": False, "response": {"status_code": 503, "body": "not ready"},
                "error_type": "assertion_failed"}

    def collect_logs(build_id):
        log_calls.append(build_id)
        assert build_id == second.build_id
        return {"web": "failed build-owned container: dependency unavailable"}

    def debug_agent(role, context, *, cwd):
        debug_calls.append(deepcopy(context))
        assert Path(cwd).is_dir()
        assert context["build_id"] == second.build_id
        assert context["commit_id"] == second.git_commit_sha
        assert role.agent_role_version == second.dynamic_agent_role_version
        return {"retry_payload": None, "report_text": "Dependency unavailable; no safe probe retry"}

    def cleanup_failed_build(build_id):
        cleanup_calls.append(build_id)
        assert build_id == second.build_id
        # This runner uses only the harness's FakeDocker and FakeHarbor.
        return harness.runner.run(build_id)

    def reject_default_cleanup(*args, **kwargs):
        pytest.fail("The integration test must not construct a real Docker cleanup runner")

    monkeypatch.setattr("orchestrator.dynamic_checks.BuildCleanupRunner", reject_default_cleanup)
    checks = DynamicCheckService(
        repository, container.dispatcher, harness.settings, container.route_checks,
        container.builds, container.build_resources,
        executor=SimpleNamespace(run_json=debug_agent),
        probe_client=SimpleNamespace(run=failed_probe),
        log_collector=SimpleNamespace(collect=collect_logs),
    )
    with build_lease(repository, second.build_id, [CleanupStatus.NOT_STARTED]) as tracker:
        assert tracker is not None
        failed = checks.execute(second.build_id, guard=tracker.guard)
    assert failed.status is BuildStatus.FAILED
    assert failed.dynamic_status == "failed" and failed.dynamic_check_passed is False
    assert failed.teardown_required is True and failed.teardown_status == "pending"
    assert failed.probe_result["final"]["response"]["status_code"] == 503
    assert failed.dynamic_report_text == "Dependency unavailable; no safe probe retry"
    assert len(probe_calls) == len(debug_calls) == 1
    assert log_calls == [second.build_id]
    assert cleanup_calls == []

    with TestClient(create_app(harness.settings, container)) as client:
        token = client.post("/oauth2/token", json={
            "client_id": harness.settings.worker_name, "client_secret": harness.settings.worker_secret,
        }).json()["access_token"]
        headers = {"Authorization": "Bearer " + token}
        build_path = f"/api/v1/jenkins/build_info/{second.build_id}"
        pending = client.get(build_path, headers=headers)
        assert pending.status_code == 200
        assert pending.json()["teardown_status"] == "pending"
        assert pending.json()["teardown_required"] is True

        assert checks.teardown(second.build_id, cleanup=cleanup_failed_build) == {
            "build_id": second.build_id, "teardown_status": "succeeded",
        }
        completed = client.get(build_path, headers=headers)
        assert completed.status_code == 200
        assert completed.json()["status"] == "failed"
        assert completed.json()["dynamic_check_passed"] is False
        assert completed.json()["teardown_status"] == "succeeded"
        assert completed.json()["teardown_required"] is False
        resources = client.get(f"/api/v1/jenkins/build_resources/{second.build_id}", headers=headers)
        assert resources.status_code == 200
        assert resources.json()["cleanup_status"] == "succeeded"

    assert cleanup_calls == [second.build_id]
    assert checks.sync_teardown(second.build_id)["teardown_status"] == "succeeded"
    assert repository.get_build(first.build_id) == first_build
    assert repository.get_build_resources(first.build_id) == first_manifest
    assert harness.docker.services.get(first_deployment.services[0].service_id)
    with pytest.raises(NotFound):
        harness.docker.services.get(second_deployment.services[0].service_id)
    assert len(harness.docker.volumes.items) == len(harness.docker.networks.items) == len(harness.docker.containers.items) == 1
    image = next(iter(harness.docker.images.items.values()))
    assert image.tags == [first_deployment.services[0].image]
    assert harness.harbor.removed == [second_deployment.services[0].image]
    assert container.workspace.snapshot_path("demo", first.build_id).exists()
    assert not container.workspace.snapshot_path("demo", second.build_id).exists()
    assert container.applications.get_app("demo")


def test_partial_failure_retries_only_remaining_resources(harness):
    build, _ = deploy(harness)
    harness.container.build_resources.request_cleanup(build.build_id)
    harness.docker.volumes.fail_remove = True
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert harness.container.repository.get_build_resources(build.build_id).registry_storage_status == "waiting_gc"
    harbor_calls = len(harness.harbor.removed)
    harness.docker.volumes.fail_remove = False
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "succeeded"
    assert len(harness.harbor.removed) == harbor_calls


def test_running_build_cannot_be_cleaned_and_cleanup_lease_prevents_deployment(harness):
    build = harness.container.builds.queue_build("demo", BuildCreate())
    with pytest.raises(ResourceConflict, match="still queued"):
        harness.container.build_resources.request_cleanup(build.build_id)
    harness.container.repository.update_build(build.build_id, {"status": BuildStatus.FAILED})
    with build_lease(harness.container.repository, build.build_id, [CleanupStatus.NOT_STARTED]) as tracker:
        assert tracker is not None
        with pytest.raises(ResourceConflict, match="still running"):
            harness.container.build_resources.request_cleanup(build.build_id)
    harness.container.build_resources.request_cleanup(build.build_id)
    with build_lease(harness.container.repository, build.build_id, [CleanupStatus.NOT_STARTED]) as tracker:
        assert tracker is None


def test_concurrent_cleanup_requests_dispatch_once(harness):
    build, _ = deploy(harness)
    def request_cleanup():
        try:
            return harness.container.build_resources.request_cleanup(build.build_id)[1]
        except ResourceConflict:
            return 409
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: request_cleanup(), range(16)))
    assert set(results) <= {202, 409}
    assert harness.container.dispatcher.cleanup_build_ids == [build.build_id]


def test_concurrent_port_reservations_have_one_owner(harness):
    repository = harness.container.repository
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda owner: repository.reserve_port("docker:test", "tcp", 18000, str(owner), "web"), range(32)))
    assert sum(results) == 1


@pytest.mark.parametrize("volumes", [{"data": {"external": True}}, {"data": {"driver_opts": {"device": "/shared"}}}])
def test_shared_volume_backends_are_rejected_before_resource_creation(harness, volumes):
    build = harness.container.builds.queue_build("demo", BuildCreate())
    compose = {**harness.compose, "volumes": volumes}
    tracker = ResourceTracker(harness.container.repository, build.build_id)
    with pytest.raises(DockerServiceError, match="Shared/external"):
        harness.adapter.deploy_build("demo", build.build_id, compose, tracker)
    assert harness.docker.volumes.items == {}
    assert harness.docker.services.items == {}


def test_input_configuration_is_frozen_and_workspace_is_read_only(harness):
    root = harness.container.workspace.ensure_app("demo")
    (root / "config.txt").write_text("original")
    build = harness.container.builds.queue_build("demo", BuildCreate())
    harness.container.applications.update_app("demo", UserAppUpdate(repository_url="https://git.example/changed.git", environment={"VALUE": "changed"}))
    manifest = harness.container.repository.get_build_resources(build.build_id)
    assert manifest.repository_url == "https://git.example/demo.git"
    assert harness.container.secret_box.decrypt_environment(manifest.environment_ciphertext) == {"VALUE": "original"}
    resolved = harness.container.workspace.resolve_build_compose("demo", build.build_id, {"services": {"web": {"volumes": [".:/input"]}}})
    assert resolved["services"]["web"]["volumes"][0].endswith(":/input:ro")


def test_workspace_cleanup_refuses_symlinks_and_preserves_app_staging(harness):
    root = harness.container.workspace.ensure_app("demo")
    (root / "config.txt").write_text("original")
    build = harness.container.builds.queue_build("demo", BuildCreate())
    snapshot = harness.container.workspace.snapshot_path("demo", build.build_id)
    (snapshot / "escape").symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        harness.container.workspace.delete_build_snapshot("demo", build.build_id, str(snapshot))
    assert (root / "config.txt").read_text() == "original"


def test_api_only_requires_build_id_and_retains_history(harness):
    build, _ = deploy(harness)
    application = create_app(harness.settings, harness.container)
    with TestClient(application) as client:
        token = client.post("/oauth2/token", json={"client_id": "local-worker", "client_secret": "local-worker-secret"}).json()["access_token"]
        headers = {"Authorization": "Bearer " + token}
        path = f"/api/v1/jenkins/build_resources/{build.build_id}"
        assert client.delete(path).status_code == 401
        report = client.get(path, headers=headers).json()
        assert report["isolated"] is True and report["cleanup_allowed"] is True
        assert "environment_ciphertext" not in report and "repository_url" not in report
        assert client.delete(path, headers=headers).status_code == 202
        assert harness.runner.run(build.build_id)["cleanup_status"] == "succeeded"
        cleaned = client.get(path, headers=headers).json()
        assert cleaned["cleanup_status"] == "succeeded"
        assert cleaned["finished_at"].endswith("+08:00")
        assert all(not service["access_available"] for service in cleaned["services"])
        assert client.delete(path, headers=headers).status_code == 200
        assert client.get(f"/api/v1/jenkins/build_info/{build.build_id}", headers=headers).status_code == 200


def test_expired_cleanup_lease_recovers_without_duplicate_resource_creation(harness):
    build, _ = deploy(harness)
    harness.container.build_resources.request_cleanup(build.build_id)
    harness.container.repository.update_build_resources(build.build_id, {
        "cleanup_status": CleanupStatus.RUNNING, "lease_owner": "dead-worker", "lease_expires_at": utc_now() - timedelta(seconds=1),
    })
    assert harness.container.build_resources.recover() == 1
    assert harness.runner.run(build.build_id)["cleanup_status"] == "succeeded"


def test_changed_resource_owner_is_protected(harness):
    build, deployment = deploy(harness)
    service = harness.docker.services.get(deployment.services[0].service_id)
    service.attrs["Spec"]["Labels"]["io.docker-jenkins-orchestrator.build-id"] = "f" * 32
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert harness.docker.services.get(service.id) is service
    assert harness.docker.volumes.items


def test_partial_deployment_failure_can_be_cleaned_without_changing_first_build(harness, monkeypatch):
    first, first_deployment = deploy(harness)
    first_state = harness.container.repository.get_build_resources(first.build_id)
    build = harness.container.builds.queue_build("demo", BuildCreate())
    compose = deepcopy(harness.compose)
    compose["services"]["web"]["image"] = f"harbor.example/apps/demo:{build.build_id}-web"

    def fail_readiness(*args):
        raise DockerServiceError("Readiness failed")

    monkeypatch.setattr(harness.adapter, "_wait_for_service_ready", fail_readiness)
    with build_lease(harness.container.repository, build.build_id, [CleanupStatus.NOT_STARTED]) as tracker:
        with pytest.raises(DockerServiceError, match="Readiness failed"):
            harness.adapter.deploy_build("demo", build.build_id, compose, tracker)
    harness.container.repository.update_build(build.build_id, {"status": BuildStatus.FAILED})
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "succeeded"
    assert harness.docker.services.get(first_deployment.services[0].service_id)
    assert harness.container.repository.get_build_resources(first.build_id) == first_state


def test_port_reservation_is_recovered_after_crash_before_manifest_registration(harness):
    build = harness.container.builds.queue_build("demo", BuildCreate())
    repository = harness.container.repository
    repository.reserve_port("docker:engine", "tcp", 18123, build.build_id, "crashed-service:8080")
    repository.update_build(build.build_id, {"status": BuildStatus.FAILED})
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "succeeded"
    assert repository.list_build_ports(build.build_id) == []


def test_same_name_replacement_is_not_deleted_even_with_copied_labels(harness):
    build, deployment = deploy(harness)
    service = harness.docker.services.get(deployment.services[0].service_id)
    service.id = "replacement-native-id"
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert harness.docker.services.get(service.id) is service


def test_unrelated_container_mount_blocks_workspace_deletion(harness):
    build, _ = deploy(harness)
    snapshot = harness.container.workspace.snapshot_path("demo", build.build_id)
    unrelated = FakeObject(harness.docker.containers, "unrelated", {"Config": {"Labels": {}, "Image": "base"},
        "Mounts": [{"Type": "bind", "Source": str(snapshot / "files")}]})
    harness.docker.containers.items[unrelated.name] = unrelated
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert snapshot.exists()
    assert harness.docker.containers.get(unrelated.id) is unrelated


@pytest.mark.parametrize("role", ["api", "worker"])
def test_platform_workspace_management_mount_does_not_block_build_cleanup(harness, role):
    build, _ = deploy(harness)
    labels = {
        "com.docker.compose.service": role,
        "io.docker-jenkins-orchestrator.workspace-manager": role,
        "io.docker-jenkins-orchestrator.worker-name": harness.settings.worker_name,
    }
    manager = FakeObject(harness.docker.containers, "platform-" + role, {
        "Config": {"Labels": labels},
        "Mounts": [{"Type": "bind", "Source": str(harness.settings.workspace_host_root), "Destination": str(harness.settings.workspace_root)}],
    })
    harness.docker.containers.items[manager.name] = manager
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "succeeded"
    assert not harness.container.workspace.snapshot_path("demo", build.build_id).exists()
    assert harness.docker.containers.get(manager.id) is manager


@pytest.mark.parametrize("mismatch", ["unlabelled", "worker", "destination", "snapshot"])
def test_workspace_mount_exemption_does_not_cover_unrelated_consumers(harness, mismatch):
    build, _ = deploy(harness)
    snapshot = harness.container.workspace.snapshot_path("demo", build.build_id)
    labels = {
        "com.docker.compose.service": "worker",
        "io.docker-jenkins-orchestrator.workspace-manager": "worker",
        "io.docker-jenkins-orchestrator.worker-name": harness.settings.worker_name,
    }
    mount = {"Type": "bind", "Source": str(harness.settings.workspace_host_root), "Destination": str(harness.settings.workspace_root)}
    if mismatch == "unlabelled":
        labels = {}
    elif mismatch == "worker":
        labels["io.docker-jenkins-orchestrator.worker-name"] = "another-platform"
    elif mismatch == "destination":
        mount["Destination"] = "/unrelated"
    else:
        mount["Source"] = str(snapshot / "files")
    consumer = FakeObject(harness.docker.containers, "unrelated", {"Config": {"Labels": labels}, "Mounts": [mount]})
    harness.docker.containers.items[consumer.name] = consumer
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert snapshot.exists()


def test_external_execution_is_rechecked_before_any_cleanup_mutation(harness):
    build, deployment = deploy(harness)
    repository = harness.container.repository
    repository.update_build(build.build_id, {"jenkins_build_number": 1, "jenkins_queue_url": "https://jenkins.example/queue/item/1/"})
    execution = SimpleNamespace(building=False)
    harness.container.build_resources.jenkins_factory = lambda: SimpleNamespace(
        get_build=lambda number: JenkinsBuild(number, execution.building, None if execution.building else "SUCCESS"))
    harness.container.build_resources.request_cleanup(build.build_id)
    execution.building = True
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert harness.docker.services.get(deployment.services[0].service_id)
    assert harness.docker.volumes.items


def test_unknown_jenkins_trigger_outcome_blocks_cleanup(harness):
    build, _ = deploy(harness)
    harness.container.repository.update_build_resources(build.build_id, {"jenkins_trigger_pending": True})
    with pytest.raises(ResourceConflict, match="outcome is unknown"):
        harness.container.build_resources.request_cleanup(build.build_id)
    assert not harness.container.dispatcher.cleanup_build_ids


def test_cleanup_dispatch_failure_is_recorded_and_retryable(harness, monkeypatch):
    build, _ = deploy(harness)
    dispatch = harness.container.dispatcher.dispatch_build_cleanup

    def unavailable(build_id):
        raise RuntimeError("redis://user:secret@queue")

    monkeypatch.setattr(harness.container.dispatcher, "dispatch_build_cleanup", unavailable)
    with pytest.raises(RepositoryError, match="enqueue"):
        harness.container.build_resources.request_cleanup(build.build_id)
    manifest = harness.container.repository.get_build_resources(build.build_id)
    assert manifest.cleanup_status == CleanupStatus.PARTIAL_FAILED
    assert "secret" not in manifest.last_error
    monkeypatch.setattr(harness.container.dispatcher, "dispatch_build_cleanup", dispatch)
    assert harness.container.build_resources.request_cleanup(build.build_id)[1] == 202
    assert harness.runner.run(build.build_id)["cleanup_status"] == "succeeded"


def test_historical_app_resources_are_protected_and_history_is_retained(harness):
    repository = harness.container.repository
    build = BuildJob(build_id="f" * 32, appid="demo", git_ref="main", status=BuildStatus.SUCCEEDED,
                     compose_snapshot=deepcopy(harness.compose))
    repository.create_build(build)
    repository.save_service(DeploymentService(appid="demo", build_id=build.build_id, service_id="shared-id", service_name="shared", image="base:latest", status="running"))
    manifest = harness.container.build_resources.manifest(build.build_id)
    assert manifest.ownership_version == 0
    assert all(not resource.managed for resource in manifest.resources.values())
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert all(resource.state == ResourceState.BLOCKED for resource in repository.get_build_resources(build.build_id).resources.values())
    assert repository.list_services("demo")[0].status == "running"


@pytest.mark.parametrize("environment", [{"DATABASE_URL": "postgresql://user:secret@shared.example/db"},
    {"REDIS_HOST": "shared.example"}, {"S3_BUCKET": "shared-data"}, {"DATABASE_URL": "${EXTERNAL_DATABASE}"}])
def test_external_stateful_configuration_is_blocked_without_exposing_secrets(harness, environment):
    compose = deepcopy(harness.compose)
    compose["services"]["web"]["environment"] = environment
    with pytest.raises(DockerServiceError) as error:
        harness.adapter.validate_build_compose(compose)
    assert "secret" not in str(error.value)
    assert not harness.docker.services.items


def test_internal_stateful_configuration_is_allowed(harness):
    compose = deepcopy(harness.compose)
    compose["services"]["db"] = {"image": "postgres:18"}
    compose["services"]["web"]["environment"] = {"DATABASE_URL": "${DATABASE_URL}"}
    harness.adapter.validate_build_compose(compose, environment={"DATABASE_URL": "postgresql://user:secret@db/db"})


def test_unknown_execution_target_is_reported_not_assumed_clean(harness):
    build, _ = deploy(harness)
    tracker = ResourceTracker(harness.container.repository, build.build_id)
    tracker.record("local_image", f"harbor.example/apps/demo:{build.build_id}-builder", target="docker:unknown")
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    manifest = harness.container.repository.get_build_resources(build.build_id)
    assert "unavailable" in manifest.last_error


def test_cleanup_waits_for_asynchronous_service_tasks(harness, monkeypatch):
    build, deployment = deploy(harness)
    service = harness.docker.services.get(deployment.services[0].service_id)
    original_remove = service.remove

    def remove_service(**kwargs):
        original_remove(**kwargs)
        for container in harness.docker.containers.items.values():
            container.status = "running"

    monkeypatch.setattr(service, "remove", remove_service)
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert harness.docker.volumes.items
    assert harness.docker.containers.items
    for container in harness.docker.containers.items.values():
        container.status = "exited"
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "succeeded"


def test_recreated_volume_with_same_name_and_labels_is_protected(harness):
    build, _ = deploy(harness)
    volume = next(iter(harness.docker.volumes.items.values()))
    volume.attrs["CreatedAt"] = "replacement-creation-time"
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert harness.docker.volumes.get(volume.name) is volume


def test_standalone_container_port_is_not_reallocated_or_released(harness):
    unrelated = FakeObject(harness.docker.containers, "standalone", {"Config": {"Labels": {}, "Image": "base"},
        "HostConfig": {"PortBindings": {"8080/tcp": [{"HostPort": "18080"}]}}})
    harness.docker.containers.items[unrelated.name] = unrelated
    build, deployment = deploy(harness)
    assert deployment.services[0].ports[0].published_port != 18080
    port = deployment.services[0].ports[0].published_port
    unrelated.attrs["HostConfig"]["PortBindings"]["8080/tcp"][0]["HostPort"] = str(port)
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert harness.container.repository.list_build_ports(build.build_id)
    assert harness.docker.containers.get(unrelated.id) is unrelated


def test_api_reports_external_build_conflict_and_missing_build(harness):
    build, _ = deploy(harness)
    harness.container.repository.update_build(build.build_id, {"jenkins_build_number": 1})
    harness.container.build_resources.jenkins_factory = lambda: SimpleNamespace(get_build=lambda number: JenkinsBuild(number, True, None))
    with TestClient(create_app(harness.settings, harness.container)) as client:
        token = client.post("/oauth2/token", json={"client_id": "local-worker", "client_secret": "local-worker-secret"}).json()["access_token"]
        headers = {"Authorization": "Bearer " + token}
        path = "/api/v1/jenkins/build_resources/" + build.build_id
        assert client.get(path, headers=headers).json()["cleanup_allowed"] is False
        assert client.delete(path, headers=headers).status_code == 409
        assert client.get("/api/v1/jenkins/build_resources/" + "0" * 32, headers=headers).status_code == 404
        assert client.delete("/api/v1/jenkins/build_resources/" + "0" * 32, headers=headers).status_code == 404


def test_builder_resource_report_is_verified_and_cleanup_retries_do_not_redelete_tags(harness):
    build, deployment = deploy(harness)
    harness.container.repository.update_build(build.build_id, {"jenkins_build_number": 1, "jenkins_queue_url": "https://jenkins.example/queue/item/1/"})
    report = {"build_id": build.build_id, "verification_complete": True, "workspace_removed": True,
        "plan_created": True, "builder_target": "docker:engine", "images": [{"reference": deployment.services[0].image, "local_removed": True}]}
    harness.container.build_resources.jenkins_factory = lambda: SimpleNamespace(get_build=lambda number: JenkinsBuild(number, False, "SUCCESS"),
        get_resource_artifact=lambda number: report)
    harness.docker.volumes.fail_remove = True
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    calls = list(harness.harbor.removed)
    harness.docker.volumes.fail_remove = False
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "succeeded"
    assert harness.harbor.removed == calls


def test_same_build_retry_preserves_native_resources_and_reserved_ports(harness, monkeypatch):
    build, deployment = deploy(harness)
    service = harness.docker.services.get(deployment.services[0].service_id)
    port = deployment.services[0].ports[0].published_port
    container = next(iter(harness.docker.containers.items.values()))
    container.attrs["HostConfig"] = {"PortBindings": {"8080/tcp": [{"HostPort": str(port)}]}}
    monkeypatch.setattr(service, "update", lambda **kwargs: None)
    compose = deepcopy(harness.compose)
    compose["services"]["web"]["image"] = deployment.services[0].image
    with build_lease(harness.container.repository, build.build_id, [CleanupStatus.NOT_STARTED]) as tracker:
        retry = harness.adapter.deploy_build("demo", build.build_id, compose, tracker)
    assert retry.network_name == deployment.network_name
    assert retry.services[0].service_id == service.id
    assert retry.services[0].ports[0].published_port == port
    assert len(harness.docker.services.items) == len(harness.docker.volumes.items) == 1


def test_private_object_store_bucket_is_allowed_only_with_internal_endpoint(harness):
    compose = deepcopy(harness.compose)
    compose["services"]["object-store"] = {"image": "minio:test"}
    compose["services"]["web"]["environment"] = {"S3_BUCKET": "data", "S3_ENDPOINT": "http://object-store:9000"}
    harness.adapter.validate_build_compose(compose)
    compose["services"]["web"]["environment"]["S3_ENDPOINT"] = "https://shared.example"
    with pytest.raises(DockerServiceError):
        harness.adapter.validate_build_compose(compose)


def test_scaled_to_zero_service_reference_protects_volume(harness):
    build, _ = deploy(harness)
    volume = next(iter(harness.docker.volumes.items.values()))
    unrelated = FakeObject(harness.docker.services, "protected-service", {"Spec": {"Labels": {}, "TaskTemplate": {
        "ContainerSpec": {"Image": "unrelated:test", "Mounts": [{"Type": "volume", "Source": volume.name}]}}}})
    harness.docker.services.items[unrelated.name] = unrelated
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert harness.docker.volumes.get(volume.name) is volume
    assert harness.docker.services.get(unrelated.id) is unrelated


def test_cleanup_reclaims_registered_builder_tag_but_retains_shared_image_content(harness):
    build, deployment = deploy(harness)
    reference = deployment.services[0].image
    builder = FakeDocker()
    builder.info = lambda: {"ID": "builder-engine", "Swarm": {}}
    image = builder.images.ensure("base:shared")
    builder.images.ensure(reference)
    harness.settings.cleanup_docker_targets = {"docker:builder-engine": "tcp://controlled-builder:2376"}
    cleaner = DockerBuildCleaner(harness.settings, harness.container.repository, client=harness.docker, client_factory=lambda endpoint: builder)
    harness.runner.docker_factory = lambda: cleaner
    harness.container.repository.update_build(build.build_id, {"jenkins_build_number": 1})
    report = {"build_id": build.build_id, "verification_complete": True, "workspace_removed": True, "plan_created": True,
        "builder_target": "docker:builder-engine", "images": [{"reference": reference, "native_id": image.id, "local_removed": False}]}
    harness.container.build_resources.jenkins_factory = lambda: SimpleNamespace(get_build=lambda number: JenkinsBuild(number, False, "SUCCESS"),
        get_resource_artifact=lambda number: report)
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "succeeded"
    assert image.tags == ["base:shared"]


def test_final_resource_verification_prevents_false_cleanup_success(harness, monkeypatch):
    build, _ = deploy(harness)
    cleaner = harness.runner.docker_factory()
    remove = cleaner.remove

    def report_without_removing(manifest, resource, tracker):
        if resource.kind != "network":
            remove(manifest, resource, tracker)

    monkeypatch.setattr(cleaner, "remove", report_without_removing)
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "partial_failed"
    assert "services or networks" in harness.container.repository.get_build_resources(build.build_id).last_error
    monkeypatch.setattr(cleaner, "remove", remove)
    harness.container.build_resources.request_cleanup(build.build_id)
    assert harness.runner.run(build.build_id)["cleanup_status"] == "succeeded"
