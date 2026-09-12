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
    assert manifest["project"] == project
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"])
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["source_compose_commit"])
    assert manifest["source_compose_path"].endswith((".yml", ".yaml"))
    assert isinstance(manifest["source_service_count"], int) and manifest["source_service_count"] > 0
    assert isinstance(manifest["source_swarm_compatible"], bool)
    assert isinstance(manifest["root_compose_present"], bool)


@pytest.mark.parametrize("project", PROJECTS)
def test_project_fixture_records_all_user_role_outputs(project: str) -> None:
    path = FIXTURES / project
    expected_files = {"manifest.json", "source-observation.md", "user-compose.yml", "user-output.md"}
    assert expected_files <= {item.name for item in path.iterdir()}
    manifest, compose = load_project(project)
    assert manifest["service_count"] == len(compose["services"])
    assert manifest["source_service_count"] == manifest["service_count"]
    output = (path / "user-output.md").read_text(encoding="utf-8").lower()
    assert "user role" in output
    assert "user-compose.yml" in output
    assert "system feedback" in output


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
        ("plane", {"services": {"web": {"image": "plane/web:source", "build": {"context": "."}}}}),
        ("paperless-ngx", {"services": {"webserver": {"image": "paperless:test", "env_file": ["docker-compose.env"]}}}),
        ("umami", {"services": {"umami": {"image": "umami:source", "init": True}}}),
        (
            "searxng",
            {"services": {"core": {"image": "searxng:source", "container_name": "searxng-core", "env_file": [".env"]}}},
        ),
        (
            "open-webui",
            {
                "services": {
                    "open-webui": {
                        "image": "open-webui:source",
                        "build": {"context": ".", "dockerfile": "Dockerfile"},
                    }
                }
            },
        ),
        (
            "linkwarden",
            {"services": {"linkwarden": {"image": "linkwarden:source", "env_file": [".env"], "volumes": ["./data:/data"]}}},
        ),
    ],
)
def test_unportable_project_shape_requires_user_role_adjustment(project: str, compose: dict[str, Any] | None) -> None:
    settings = Settings(
        environment="test",
        storage_backend="memory",
        task_dispatcher="memory",
        harbor_url="https://harbor.example",
    )
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
        if compose is None:
            with pytest.raises(BuildInputError, match="Compose document"):
                container.builds.queue_build(f"{project}-raw", BuildCreate())
            assert container.repository.list_active_builds() == []
            assert getattr(container.dispatcher, "build_ids", []) == []
            return

        # Source Compose may legitimately contain a build context or env_file;
        # Jenkins has the repository/filesystem context needed to process it.
        # The Swarm boundary is checked against the final artifact instead.
        build = container.builds.queue_build(f"{project}-raw", BuildCreate())
        artifact = JenkinsResultArtifact(
            images=("example/raw:failed",),
            compose=compose,
            _payload={"images": ["example/raw:failed"], "compose": compose},
        )
        docker = FakeDocker()
        runtime = WorkerRuntime(
            settings,
            repository=container.repository,
            build_service=container.builds,
            catalog=container.catalog,
            jenkins=FakeJenkins(artifact),
            docker_services=DockerSwarmAdapter(docker_client=docker),
            poll_scheduler=lambda *_: None,
        )
        started = runtime.start_build(build.build_id, task_id=f"start-{project}")
        assert started["status"] == "building", container.repository.get_build(build.build_id).model_dump()
        result = runtime.poll_build(build.build_id, attempt=0, task_id=f"poll-{project}")
        assert result == {"build_id": build.build_id, "status": "failed"}
        stored = container.repository.get_build(build.build_id)
        assert stored is not None and stored.error is not None
        assert any(fragment in stored.error for fragment in ("unsupported", "env_file", "relative bind"))
        assert docker.networks.items == {}
        assert docker.services.create_calls == []
    finally:
        container.close()


def test_planka_source_shape_is_already_structurally_swarm_compatible() -> None:
    """Some upstream Compose files need secret/value hardening, not field translation."""

    compose = {
        "services": {
            "planka": {
                "image": "ghcr.io/plankanban/planka:latest",
                "environment": [
                    "BASE_URL=http://localhost:3000",
                    "DATABASE_URL=postgresql://postgres@postgres/planka",
                    "SECRET_KEY=notsecretkey",
                ],
                "ports": ["3000:1337"],
                "depends_on": {"postgres": {"condition": "service_healthy"}},
                "restart": "on-failure",
            },
            "postgres": {
                "image": "postgres:16-alpine",
                "environment": ["POSTGRES_DB=planka", "POSTGRES_HOST_AUTH_METHOD=trust"],
                "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U postgres -d planka"]},
                "volumes": ["db-data:/var/lib/postgresql/data"],
                "restart": "on-failure",
            },
        },
        "volumes": {"db-data": {}},
    }
    DockerSwarmAdapter.validate_compose(compose)


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
