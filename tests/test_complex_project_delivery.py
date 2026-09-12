from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest
import yaml
from docker.models.services import _get_create_service_kwargs

from orchestrator.container import create_container
from orchestrator.docker_services import DockerServiceError, DockerSwarmAdapter
from orchestrator.jenkins import JenkinsBuild, JenkinsBuildRequest, JenkinsQueueItem, JenkinsResultArtifact
from orchestrator.models import BuildCreate, BuildStatus, UserAppCreate, UserAppUpdate
from orchestrator.services import BuildInputError
from orchestrator.worker_runtime import WorkerRuntime
from orchestrator.config import Settings


FIXTURES = Path(__file__).parent / "fixtures" / "complex_projects"
PROJECTS = tuple(sorted(path.name for path in FIXTURES.iterdir() if path.is_dir()))


class NotFound(Exception):
    status_code = 404


class FakeNetwork:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = f"network-{name}"


class FakeNetworks:
    def __init__(self) -> None:
        self.items: dict[str, FakeNetwork] = {}

    def get(self, name: str) -> FakeNetwork:
        if name not in self.items:
            raise NotFound(name)
        return self.items[name]

    def create(self, name: str, **_: Any) -> FakeNetwork:
        network = FakeNetwork(name)
        self.items[name] = network
        return network


class FakeService:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = f"service-{name}"
        self.removed = False

    def update(self, **_: Any) -> None:
        return None

    def remove(self) -> None:
        self.removed = True


class FakeServices:
    def __init__(self) -> None:
        self.items: dict[str, FakeService] = {}
        self.create_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, name: str) -> FakeService:
        if name not in self.items:
            raise NotFound(name)
        return self.items[name]

    def create(self, image: str, **kwargs: Any) -> FakeService:
        service = FakeService(kwargs["name"])
        self.items[service.name] = service
        self.create_calls.append((image, kwargs))
        return service

    def list(self, **_: Any) -> list[FakeService]:
        return list(self.items.values())


class FakeDocker:
    def __init__(self) -> None:
        self.networks = FakeNetworks()
        self.services = FakeServices()


class FakeJenkins:
    def __init__(self, artifact: JenkinsResultArtifact) -> None:
        self.artifact = artifact
        self.requests: list[JenkinsBuildRequest] = []

    def trigger_build(self, request: JenkinsBuildRequest) -> JenkinsQueueItem:
        self.requests.append(request)
        return JenkinsQueueItem("https://jenkins.example/queue/item/1/", 1)

    def get_queue_item(self, queue_url: str) -> JenkinsQueueItem:
        assert queue_url.endswith("/queue/item/1/")
        return JenkinsQueueItem(queue_url, 1, build_number=42)

    def get_build(self, number: int) -> JenkinsBuild:
        assert number == 42
        return JenkinsBuild(number=number, building=False, result="SUCCESS")

    def get_result_artifact(self, number: int) -> JenkinsResultArtifact:
        assert number == 42
        return self.artifact


def load_project(project: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = FIXTURES / project
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    compose = yaml.safe_load((path / "user-compose.yml").read_text(encoding="utf-8"))
    return manifest, compose


@pytest.mark.parametrize("project", PROJECTS)
def test_project_fixture_has_pinned_upstream_provenance(project: str) -> None:
    manifest, _ = load_project(project)
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"])
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["source_compose_commit"])
    assert manifest["source_compose_path"].endswith((".yml", ".yaml"))


@pytest.mark.parametrize("project", PROJECTS)
def test_user_role_compose_delivers_each_project_through_the_generic_pipeline(project: str) -> None:
    manifest, compose = load_project(project)
    DockerSwarmAdapter.validate_compose(compose)
    images = tuple(service["image"] for service in compose["services"].values())
    artifact = JenkinsResultArtifact(
        images=images,
        compose=compose,
        _payload={"images": list(images), "compose": compose},
    )
    settings = Settings(
        environment="test",
        storage_backend="memory",
        task_dispatcher="memory",
        harbor_url="https://harbor.example",
        celery_poll_interval_seconds=1,
    )
    container = create_container(settings)
    docker = FakeDocker()
    jenkins = FakeJenkins(artifact)
    runtime = WorkerRuntime(
        settings,
        repository=container.repository,
        build_service=container.builds,
        catalog=container.catalog,
        jenkins=jenkins,
        docker_services=DockerSwarmAdapter(docker_client=docker),
        poll_scheduler=lambda *_: None,
    )
    try:
        app = container.applications.create_app(
            UserAppCreate(
                appid=project,
                name=project,
                repository_url=manifest["repository"] + ".git",
                git_ref=manifest["branch"],
            )
        )
        with pytest.raises(BuildInputError, match="Compose document"):
            container.builds.queue_build(app.appid, BuildCreate())
        container.applications.update_app(app.appid, UserAppUpdate(compose=compose))
        build = container.builds.queue_build(app.appid, BuildCreate())

        started = runtime.start_build(build.build_id, task_id=f"start-{project}")
        completed = runtime.poll_build(build.build_id, attempt=0, task_id=f"poll-{project}")

        assert started == {"build_id": build.build_id, "status": "building"}
        assert completed == {"build_id": build.build_id, "status": "succeeded"}
        stored = container.repository.get_build(build.build_id)
        assert stored is not None
        assert stored.status is BuildStatus.SUCCEEDED
        assert len(container.repository.list_services(project)) == manifest["service_count"]
        assert len(docker.services.create_calls) == manifest["service_count"]
        assert jenkins.requests[0].compose == compose
        public_ports = [port for _, kwargs in docker.services.create_calls for port in kwargs.get("endpoint_spec", {}).get("Ports", [])]
        assert any(port["PublishedPort"] == manifest["public_port"] for port in public_ports)
        for image, kwargs in docker.services.create_calls:
            # Exercise the same Docker SDK normalization used by
            # ServiceCollection.create; the fake daemon only records calls.
            _get_create_service_kwargs("create", {"image": image, **kwargs})
    finally:
        container.close()


@pytest.mark.parametrize(
    ("project", "compose"),
    [
        ("immich", None),
        ("plane", {"services": {"web": {"build": {"context": "."}}}}),
        ("paperless-ngx", {"services": {"webserver": {"image": "paperless:test", "env_file": ["docker-compose.env"]}}}),
    ],
)
def test_original_project_shape_requires_user_role_adjustment(project: str, compose: dict[str, Any] | None) -> None:
    settings = Settings(environment="test", storage_backend="memory", task_dispatcher="memory")
    container = create_container(settings)
    try:
        container.applications.create_app(
            UserAppCreate(
                appid=f"{project}-raw",
                name=f"{project} raw",
                repository_url=f"https://github.com/{project}",
                compose=compose,
            )
        )
        with pytest.raises(BuildInputError, match="(unsupported|env_file|Compose)"):
            container.builds.queue_build(f"{project}-raw", BuildCreate())
        assert container.repository.list_active_builds() == []
        assert getattr(container.dispatcher, "build_ids", []) == []
    finally:
        container.close()


@pytest.mark.parametrize("project", PROJECTS)
def test_user_compose_is_not_project_specific_to_swarm_adapter(project: str) -> None:
    _, compose = load_project(project)
    adapter = DockerSwarmAdapter
    assert not any(
        key in service
        for service in compose["services"].values()
        for key in ("env_file", "build", "container_name", "extends")
    )
    # The same class method is used for all projects; no project identifier is
    # passed into validation or feature translation.
    adapter.validate_compose(compose)
