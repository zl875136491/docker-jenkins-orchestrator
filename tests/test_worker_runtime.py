from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from orchestrator.config import Settings
from orchestrator.container import ApplicationContainer, create_container
from orchestrator.docker_services import DockerServiceError, PublishedPort as DockerPublishedPort, ServiceDeployment, SwarmDeployment
from orchestrator.gitlab import GitLabProject, GitLabRepositoryRef
from orchestrator.jenkins import (
    JenkinsArtifactError,
    JenkinsBuild,
    JenkinsBuildRequest,
    JenkinsQueueItem,
    JenkinsResultArtifact,
    JenkinsNotStartedError,
)
from orchestrator.jenkins_diagnostics import summarize_failure
from orchestrator.models import BaseImage, BuildCreate, BuildStatus, CleanupStatus, UserAppCreate, UserImage, utc_now
from orchestrator.services import BuildInputError
from orchestrator.worker_runtime import WorkerRuntime


APP_COMPOSE = {
    "version": "3.9",
    "services": {
        "api": {
            "build": {"context": ".", "dockerfile": "Dockerfile"},
            "image": "harbor.example/apps/demo:source",
            "ports": ["18080:8080"],
        }
    },
}


class FakeJenkins:
    def __init__(
        self,
        queue_items: list[JenkinsQueueItem],
        *,
        build: JenkinsBuild | None = None,
        artifact: JenkinsResultArtifact | Exception | None = None,
        console_log: str | Exception = "",
    ) -> None:
        self._queue_items = list(queue_items)
        self._build = build
        self._artifact = artifact
        self._console_log = console_log
        self.requests: list[JenkinsBuildRequest] = []
        self.queue_calls: list[str] = []
        self.build_calls: list[int] = []
        self.artifact_calls: list[int] = []
        self.failure_calls: list[int] = []

    def trigger_build(self, request: JenkinsBuildRequest) -> JenkinsQueueItem:
        self.requests.append(request)
        if request.build_id and isinstance(self._artifact, JenkinsResultArtifact):
            images = []
            for name, service in self._artifact.compose.get("services", {}).items():
                image = f"{request.image_repository}:{request.build_id}-{name}"
                if service.get("image"):
                    service["image"] = image
                images.append(image)
            payload = {**self._artifact._payload, "build_id": request.build_id, "images": images, "compose": self._artifact.compose,
                       "image_digests": {image: "sha256:" + "a" * 64 for image in images},
                       "source": {"actual_commit": request.git_ref if len(request.git_ref) == 40 else "e" * 40}}
            self._artifact = JenkinsResultArtifact(images=tuple(images), compose=self._artifact.compose, _payload=payload)
        return JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42)

    def get_queue_item(self, queue_url: str) -> JenkinsQueueItem:
        self.queue_calls.append(queue_url)
        return self._queue_items.pop(0)

    def get_build(self, number: int) -> JenkinsBuild:
        self.build_calls.append(number)
        assert self._build is not None
        return self._build

    def get_result_artifact(self, number: int) -> JenkinsResultArtifact:
        self.artifact_calls.append(number)
        if isinstance(self._artifact, Exception):
            raise self._artifact
        assert self._artifact is not None
        return self._artifact

    def get_failure_summary(self, number: int, *, sensitive_values=()) -> str:
        self.failure_calls.append(number)
        if isinstance(self._console_log, Exception):
            raise self._console_log
        return summarize_failure(self._console_log, sensitive_values=sensitive_values)


class FakeGitLab:
    def __init__(self, commit_sha: str) -> None:
        self.commit_sha = commit_sha
        self.calls: list[tuple[str, str]] = []

    def validate_repository_ref(self, repository_url: str, git_ref: str) -> GitLabRepositoryRef:
        self.calls.append((repository_url, git_ref))
        return GitLabRepositoryRef(
            project=GitLabProject(project_id=1, path_with_namespace="team/demo"),
            git_ref=git_ref,
            commit_sha=self.commit_sha,
            repository_url=repository_url,
        )


class FakeDockerServices:
    def __init__(self, deployment: SwarmDeployment | Exception) -> None:
        self._deployment = deployment
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def deploy(self, appid: str, compose: dict[str, Any]) -> SwarmDeployment:
        self.calls.append((appid, compose))
        if isinstance(self._deployment, Exception):
            raise self._deployment
        return self._deployment

    def deploy_build(self, appid: str, build_id: str, compose: dict[str, Any], tracker) -> SwarmDeployment:
        tracker.guard()
        return self.deploy(appid, compose)


class FakeRegistry:
    def __init__(self) -> None:
        self.sources: list[str] = []

    def sync(self, source: str) -> BaseImage:
        self.sources.append(source)
        return BaseImage(
            source_image=source,
            harbor_reference=f"harbor.example/boot-images/{source}",
            status="synced",
        )


@dataclass
class RuntimeHarness:
    container: ApplicationContainer
    runtime: WorkerRuntime
    scheduled_starts: list[str]
    scheduled_polls: list[tuple[str, int, int]]


def make_harness(
    *,
    jenkins: FakeJenkins | None = None,
    gitlab: FakeGitLab | None = None,
    docker_services: FakeDockerServices | None = None,
    registry: FakeRegistry | None = None,
) -> RuntimeHarness:
    settings = Settings(
        environment="test",
        storage_backend="memory",
        task_dispatcher="memory",
        harbor_url="https://harbor.example/",
        celery_poll_interval_seconds=7,
    )
    container = create_container(settings)
    scheduled_starts: list[str] = []
    scheduled_polls: list[tuple[str, int, int]] = []
    runtime = WorkerRuntime(
        settings,
        repository=container.repository,
        build_service=container.builds,
        catalog=container.catalog,
        jenkins=jenkins,
        gitlab=gitlab,
        docker_services=docker_services,
        registry=registry,
        start_scheduler=scheduled_starts.append,
        poll_scheduler=lambda build_id, attempt, countdown: scheduled_polls.append((build_id, attempt, countdown)),
    )
    return RuntimeHarness(
        container=container,
        runtime=runtime,
        scheduled_starts=scheduled_starts,
        scheduled_polls=scheduled_polls,
    )


def queue_build(harness: RuntimeHarness, appid: str = "demo", *, compose: dict | None = None):
    compose = deepcopy(APP_COMPOSE if compose is None else compose)
    for service in compose.get("services", {}).values():
        if isinstance(service, dict):
            service.setdefault("build", {"context": ".", "dockerfile": "Dockerfile"})
    harness.container.applications.create_app(
        UserAppCreate(
            appid=appid,
            name="Demo",
            repository_url=f"https://git.example/{appid}.git",
            git_ref="main",
            environment={"DEPLOY_TOKEN": "not-exposed"},
                compose=compose,
        )
    )
    return harness.container.builds.queue_build(appid, BuildCreate())


def successful_artifact() -> JenkinsResultArtifact:
    compose = {
        "services": {
            "api": {
                "image": "harbor.example/apps/demo:build-42",
                "ports": ["18080:8080"],
            }
        }
    }
    return JenkinsResultArtifact(
        images=("harbor.example/apps/demo:build-42",),
        compose=compose,
        _payload={"images": ["harbor.example/apps/demo:build-42"], "compose": compose},
    )


def successful_deployment() -> SwarmDeployment:
    return SwarmDeployment(
        appid="demo",
        network_name="orchestrator-demo",
        services=(
            ServiceDeployment(
                service_id="service-demo-api",
                service_name="demo-api",
                image="harbor.example/apps/demo:build-42",
                action="created",
                endpoint="demo-api:18080",
                ports=(DockerPublishedPort(target_port=8080, published_port=18080),),
            ),
        ),
    )


def test_start_build_triggers_jenkins_and_schedules_the_first_poll() -> None:
    jenkins = FakeJenkins([])
    gitlab = FakeGitLab("a" * 40)
    harness = make_harness(jenkins=jenkins, gitlab=gitlab)
    build = queue_build(harness)

    result = harness.runtime.start_build(build.build_id, task_id="start-task")

    stored = harness.container.repository.get_build(build.build_id)
    assert result == {"build_id": build.build_id, "status": "building"}
    assert stored is not None
    assert stored.status is BuildStatus.BUILDING
    assert stored.celery_task_id == build.celery_task_id
    assert stored.git_commit_sha == "a" * 40
    assert stored.jenkins_queue_url == "https://jenkins.example/queue/item/42/"
    assert harness.scheduled_polls == [(build.build_id, 0, 7)]
    assert len(jenkins.requests) == 1
    request = jenkins.requests[0]
    assert request.appid == "demo"
    assert request.repository_url == "https://git.example/demo.git"
    assert request.git_ref == "a" * 40
    assert dict(request.environment) == {"DEPLOY_TOKEN": "not-exposed"}
    assert request.compose == APP_COMPOSE
    assert request.image_repository == "harbor.example/apps/demo"
    assert gitlab.calls == [("https://git.example/demo.git", "main")]


@pytest.mark.parametrize(
    "source_mount",
    [".:/workspace", "./:/workspace", "~/app:/workspace", {"type": "bind", "source": ".", "target": "/workspace"}],
    ids=["saved-developer-app", "relative-directory", "home-directory", "long-bind-syntax"],
)
def test_source_relative_bind_fails_before_gitlab_or_jenkins_without_changing_saved_compose(source_mount: Any) -> None:
    source = {
        "services": {
            "developer-app": {
                    "build": {"context": ".", "dockerfile": "Dockerfile"},
                    "image": "node:22.11.0-alpine3.20",
                "working_dir": "/workspace",
                "ports": ["5188:5188"],
                "volumes": [source_mount, "developer_app_node_modules:/workspace/node_modules"],
            }
        },
        "volumes": {"developer_app_node_modules": {}},
    }
    original = deepcopy(source)
    jenkins = FakeJenkins([])
    gitlab = FakeGitLab("a" * 40)
    docker_services = FakeDockerServices(successful_deployment())
    harness = make_harness(jenkins=jenkins, gitlab=gitlab, docker_services=docker_services)
    build = queue_build(harness, compose=source)

    assert build.status is BuildStatus.QUEUED
    with pytest.raises(BuildInputError, match="relative bind volume paths are not supported for Swarm services"):
        WorkerRuntime._validate_source_compose(source)
    result = harness.runtime.start_build(build.build_id, task_id="start-task")

    stored = harness.container.repository.get_build(build.build_id)
    assert result == {"build_id": build.build_id, "status": "failed"}
    assert stored is not None
    assert stored.status is BuildStatus.FAILED
    assert stored.error == "Compose relative bind volume paths are not supported for Swarm services" or "Workspace mount source is empty" in stored.error
    assert stored.git_commit_sha is None
    assert stored.jenkins_queue_url is None
    assert stored.jenkins_build_number is None
    assert gitlab.calls == []
    assert jenkins.requests == []
    assert docker_services.calls == []
    assert harness.scheduled_polls == []
    assert source == original
    assert harness.container.repository.get_app("demo").compose == original
    assert len(harness.container.repository.list_alerts("demo")) == 1

    # A retry cannot overwrite the terminal failure or start a new external
    # build. The failed record remains available for status and diagnostics.
    assert harness.runtime.start_build(build.build_id, task_id="retry-task") == result
    assert harness.container.repository.get_build(build.build_id).error == stored.error
    assert gitlab.calls == jenkins.requests == []


@pytest.mark.parametrize(
    ("runtime_fields", "error_fragment"),
    [
        ({"ports": ["invalid"]}, "Compose service port is invalid"),
        ({"working_dir": "workspace"}, "Compose working directory is invalid"),
        ({"network_mode": "host"}, "uses unsupported fields: network_mode"),
        ({"env_file": [".env"]}, "uses env_file"),
    ],
)
def test_source_runtime_fields_are_validated_before_external_builds(runtime_fields: dict, error_fragment: str) -> None:
    source = deepcopy(APP_COMPOSE)
    source["services"]["api"].update(runtime_fields)
    jenkins = FakeJenkins([])
    gitlab = FakeGitLab("a" * 40)
    harness = make_harness(jenkins=jenkins, gitlab=gitlab)
    build = queue_build(harness, compose=source)

    result = harness.runtime.start_build(build.build_id, task_id="start-task")

    assert result == {"build_id": build.build_id, "status": "failed"}
    assert error_fragment in harness.container.repository.get_build(build.build_id).error
    assert gitlab.calls == []
    assert jenkins.requests == []
    assert harness.scheduled_polls == []


@pytest.mark.parametrize("build_config", [{"context": ".", "dockerfile": "Dockerfile"}])
@pytest.mark.parametrize("include_image", [True, False])
def test_source_build_preflight_preserves_jenkins_build_input_and_accepts_missing_image(
    build_config: str | dict, include_image: bool
) -> None:
    source = deepcopy(APP_COMPOSE)
    source["services"]["api"].update(
        {"build": build_config, "working_dir": "/workspace", "volumes": ["data:/data"]}
    )
    source["volumes"] = {"data": {}}
    if not include_image:
        source["services"]["api"].pop("image")
    original = deepcopy(source)
    artifact_compose = deepcopy(source)
    artifact_compose["services"]["api"].pop("build")
    artifact_compose["services"]["api"]["image"] = "harbor.example/apps/demo:build-42"
    artifact = JenkinsResultArtifact(
        images=("harbor.example/apps/demo:build-42",),
        compose=artifact_compose,
        _payload={"images": ["harbor.example/apps/demo:build-42"], "compose": artifact_compose},
    )
    jenkins = FakeJenkins(
        [JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42, build_number=42)],
        build=JenkinsBuild(number=42, building=False, result="SUCCESS"),
        artifact=artifact,
    )
    gitlab = FakeGitLab("a" * 40)
    docker_services = FakeDockerServices(successful_deployment())
    harness = make_harness(jenkins=jenkins, gitlab=gitlab, docker_services=docker_services)
    build = queue_build(harness, compose=source)

    started = harness.runtime.start_build(build.build_id, task_id="start-task")
    completed = harness.runtime.poll_build(build.build_id, attempt=0, task_id="poll-task")

    assert started == {"build_id": build.build_id, "status": "building"}
    assert completed == {"build_id": build.build_id, "status": "succeeded"}
    assert gitlab.calls == [("https://git.example/demo.git", "main")]
    assert jenkins.requests[0].compose == original
    assert jenkins.requests[0].compose["services"]["api"]["build"] == build_config
    assert source == original
    assert harness.container.repository.get_app("demo").compose == original
    assert docker_services.calls == [("demo", artifact_compose)]


def test_start_build_preserves_raw_appid_but_encodes_external_image_repository() -> None:
    jenkins = FakeJenkins([])
    harness = make_harness(jenkins=jenkins)
    appid = "APP-20261010-1FDF7D6A"
    build = queue_build(harness, appid)

    result = harness.runtime.start_build(build.build_id, task_id="start-task")

    assert result == {"build_id": build.build_id, "status": "building"}
    request = jenkins.requests[0]
    assert request.appid == appid
    assert request.image_repository == (
        "harbor.example/apps/appid-v1-4150502d32303236313031302d3146444637443641"
    )


def test_image_repository_rejects_harbor_url_path_query_and_invalid_project() -> None:
    harness = make_harness()
    for harbor_url in ("harbor.example/path", "harbor.example?query=1", "harbor.example#fragment"):
        harness.runtime.settings.harbor_url = harbor_url
        with pytest.raises(BuildInputError, match="Harbor registry URL|IMAGE_REPOSITORY"):
            harness.runtime._image_repository("demo")

    harness.runtime.settings.harbor_url = "harbor.example:5000"
    harness.runtime.settings.harbor_project = "Apps"
    with pytest.raises(BuildInputError, match="Harbor project"):
        harness.runtime._image_repository("demo")


@pytest.mark.parametrize(
    ("harbor_url", "registry"),
    [
        ("harbor.example:5000", "harbor.example:5000"),
        ("https://Harbor.EXAMPLE:5000/", "Harbor.EXAMPLE:5000"),
        ("http://localhost:5000", "localhost:5000"),
        ("http://[::1]:5000/", "[::1]:5000"),
    ],
)
def test_image_repository_accepts_explicit_registry_ports(harbor_url: str, registry: str) -> None:
    harness = make_harness()
    harness.runtime.settings.harbor_url = harbor_url
    assert harness.runtime._image_repository("demo-app") == f"{registry}/apps/demo-app"


@pytest.mark.parametrize(
    "harbor_url",
    [
        "https://user:password@harbor.example",
        "https://harbor.example/path",
        "https://harbor.example?",
        "https://harbor.example#",
        "harbor",
        "https://harbor.example:bad",
        "https://harbor.example:65536",
        "https://[bad]",
        "ftp://harbor.example",
    ],
)
def test_image_repository_rejects_invalid_harbor_urls_as_build_input_errors(harbor_url: str) -> None:
    harness = make_harness()
    harness.runtime.settings.harbor_url = harbor_url
    with pytest.raises(BuildInputError):
        harness.runtime._image_repository("demo")


@pytest.mark.parametrize("project", ["", "Apps", "apps_", "apps___a", "apps/nested", "a" * 255])
def test_image_repository_rejects_invalid_or_overlong_projects(project: str) -> None:
    harness = make_harness()
    harness.runtime.settings.harbor_project = project
    with pytest.raises(BuildInputError):
        harness.runtime._image_repository("demo")


def test_invalid_registry_fails_build_before_triggering_jenkins() -> None:
    jenkins = FakeJenkins([])
    harness = make_harness(jenkins=jenkins)
    harness.runtime.settings.harbor_project = "Apps"
    build = queue_build(harness)

    result = harness.runtime.start_build(build.build_id, task_id="start-task")

    assert result == {"build_id": build.build_id, "status": "failed"}
    assert jenkins.requests == []


def test_pending_queue_then_success_persists_artifact_images_and_swarm_services() -> None:
    artifact = successful_artifact()
    jenkins = FakeJenkins(
        [
            JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42),
            JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42, build_number=91),
        ],
        build=JenkinsBuild(number=91, building=False, result="SUCCESS"),
        artifact=artifact,
    )
    docker_services = FakeDockerServices(successful_deployment())
    harness = make_harness(jenkins=jenkins, docker_services=docker_services)
    build = queue_build(harness)

    harness.runtime.start_build(build.build_id, task_id="start-task")
    pending = harness.runtime.poll_build(build.build_id, attempt=0, task_id="poll-task-0")
    completed = harness.runtime.poll_build(build.build_id, attempt=1, task_id="poll-task-1")

    stored = harness.container.repository.get_build(build.build_id)
    assert pending == {"build_id": build.build_id, "status": "building"}
    assert completed == {"build_id": build.build_id, "status": "succeeded"}
    assert stored is not None
    assert stored.status is BuildStatus.SUCCEEDED
    assert stored.jenkins_build_number == 91
    assert stored.images == [f"harbor.example/apps/demo:{build.build_id}-api"]
    assert stored.service_ids == ["service-demo-api"]
    assert harness.scheduled_polls == [(build.build_id, 0, 7), (build.build_id, 1, 7)]
    assert jenkins.queue_calls == ["https://jenkins.example/queue/item/42/"] * 2
    assert jenkins.build_calls == [91]
    assert jenkins.artifact_calls == [91]
    assert [image.reference for image in harness.container.repository.list_user_images("demo")] == [
        f"harbor.example/apps/demo:{build.build_id}-api"
    ]
    assert [service.service_id for service in harness.container.repository.list_services("demo")] == ["service-demo-api"]
    stored_service = harness.container.repository.list_services("demo")[0]
    assert [port.model_dump() for port in stored_service.published_ports] == [
        {"target_port": 8080, "published_port": 18080, "protocol": "tcp", "mode": "ingress"}
    ]
    assert docker_services.calls == [("demo", artifact.compose)]


@pytest.mark.parametrize(
    ("artifact_fields", "error_fragment"),
    [
        ({"image": None}, "Jenkins deployment compose services must define images"),
        ({"build": "."}, "uses unsupported fields: build"),
        ({"volumes": [".:/workspace"]}, "changed volumes"),
    ],
    ids=["missing-final-image", "unconsumed-build-field", "invalid-artifact-mount"],
)
def test_source_preflight_does_not_replace_strict_deployment_artifact_validation(
    artifact_fields: dict, error_fragment: str
) -> None:
    artifact_compose = deepcopy(APP_COMPOSE)
    artifact_compose["services"]["api"].update(artifact_fields)
    artifact = JenkinsResultArtifact(
        images=("harbor.example/apps/demo:build-42",),
        compose=artifact_compose,
        _payload={"images": ["harbor.example/apps/demo:build-42"], "compose": artifact_compose},
    )
    jenkins = FakeJenkins(
        [JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42, build_number=42)],
        build=JenkinsBuild(number=42, building=False, result="SUCCESS"),
        artifact=artifact,
    )
    docker_services = FakeDockerServices(successful_deployment())
    harness = make_harness(jenkins=jenkins, docker_services=docker_services)
    build = queue_build(harness)

    started = harness.runtime.start_build(build.build_id, task_id="start-task")
    completed = harness.runtime.poll_build(build.build_id, attempt=0, task_id="poll-task")

    assert started == {"build_id": build.build_id, "status": "building"}
    assert completed == {"build_id": build.build_id, "status": "failed"}
    assert error_fragment in harness.container.repository.get_build(build.build_id).error
    assert docker_services.calls == []
    assert harness.container.repository.list_user_images("demo") == []
    assert harness.container.repository.list_services("demo") == []


def test_artifact_cannot_drop_source_published_ports() -> None:
    source = APP_COMPOSE
    artifact_compose = {"services": {"api": {"image": "harbor.example/apps/demo:build-43"}}}
    artifact = JenkinsResultArtifact(
        images=("harbor.example/apps/demo:build-43",),
        compose=artifact_compose,
        _payload={"images": ["harbor.example/apps/demo:build-43"], "compose": artifact_compose},
    )
    jenkins = FakeJenkins(
        [JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42, build_number=92)],
        build=JenkinsBuild(number=92, building=False, result="SUCCESS"),
        artifact=artifact,
    )
    docker_services = FakeDockerServices(successful_deployment())
    harness = make_harness(jenkins=jenkins, docker_services=docker_services)
    build = queue_build(harness)

    harness.runtime.start_build(build.build_id, task_id="start-task")
    result = harness.runtime.poll_build(build.build_id, attempt=0, task_id="poll-task")

    stored = harness.container.repository.get_build(build.build_id)
    assert result == {"build_id": build.build_id, "status": "failed"}
    assert stored is not None
    assert stored.error is not None
    assert "changed ports" in stored.error
    assert docker_services.calls == []
    assert harness.container.repository.list_services("demo") == []


@pytest.mark.parametrize(
    "change",
    [
        "missing-declarations",
        "missing-name",
        "changed-name",
        "missing-external",
        "changed-external",
        "missing-mounts",
        "changed-source",
        "changed-target",
        "changed-mode",
        "added-bind",
    ],
)
def test_artifact_cannot_change_source_volume_contract_before_deployment(change: str) -> None:
    source = deepcopy(APP_COMPOSE)
    source["services"]["api"]["volumes"] = ["data:/data"]
    source["volumes"] = {"data": {"external": False, "name": "shared-data"}}
    artifact_compose = deepcopy(source)
    artifact_compose["services"]["api"]["image"] = "harbor.example/apps/demo:build-43"
    if change == "missing-declarations":
        artifact_compose.pop("volumes")
    elif change == "missing-name":
        artifact_compose["volumes"]["data"].pop("name")
    elif change == "changed-name":
        artifact_compose["volumes"]["data"]["name"] = "other-data"
    elif change == "missing-external":
        artifact_compose["volumes"]["data"].pop("external")
    elif change == "changed-external":
        artifact_compose["volumes"]["data"]["external"] = True
    elif change == "missing-mounts":
        artifact_compose["services"]["api"].pop("volumes")
    else:
        artifact_compose["services"]["api"]["volumes"] = {
            "changed-source": ["other-data:/data"],
            "changed-target": ["data:/other-data"],
            "changed-mode": ["data:/data:ro"],
            "added-bind": ["data:/data", "/srv/other:/other-data"],
        }[change]
    artifact = JenkinsResultArtifact(
        images=("harbor.example/apps/demo:build-43",),
        compose=artifact_compose,
        _payload={"images": ["harbor.example/apps/demo:build-43"], "compose": artifact_compose},
    )
    jenkins = FakeJenkins(
        [JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42, build_number=93)],
        build=JenkinsBuild(number=93, building=False, result="SUCCESS"),
        artifact=artifact,
    )
    docker_services = FakeDockerServices(successful_deployment())
    harness = make_harness(jenkins=jenkins, docker_services=docker_services)
    build = queue_build(harness, compose=source)

    harness.runtime.start_build(build.build_id, task_id="start-task")
    result = harness.runtime.poll_build(build.build_id, attempt=0, task_id="poll-task")

    stored = harness.container.repository.get_build(build.build_id)
    assert result == {"build_id": build.build_id, "status": "failed"}
    assert stored is not None
    assert stored.error is not None and "volumes" in stored.error
    assert docker_services.calls == []
    assert harness.container.repository.list_user_images("demo") == []
    assert harness.container.repository.list_services("demo") == []
    assert len(harness.container.repository.list_alerts("demo")) == 1


@pytest.mark.parametrize("appid", ["demo", "Foo"])
@pytest.mark.parametrize("bound_template", [False, True])
def test_artifact_preserves_template_volume_identity_and_raw_owner(appid: str, bound_template: bool) -> None:
    harness = make_harness()
    source = harness.container.catalog.compose(["mongodb"], appid=appid if bound_template else None)
    artifact_compose = deepcopy(source)
    image = f"{harness.runtime._image_repository(appid)}:build-43"
    for definition in artifact_compose["services"].values():
        definition["image"] = image
        definition.pop("build", None)
    artifact = JenkinsResultArtifact(
        images=(image,),
        compose=artifact_compose,
        _payload={"images": [image], "compose": artifact_compose},
    )
    jenkins = FakeJenkins(
        [JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42, build_number=94)],
        build=JenkinsBuild(number=94, building=False, result="SUCCESS"),
        artifact=artifact,
    )
    docker_services = FakeDockerServices(replace(successful_deployment(), appid=appid))
    harness = make_harness(jenkins=jenkins, docker_services=docker_services)
    build = queue_build(harness, appid=appid, compose=source)

    harness.runtime.start_build(build.build_id, task_id="start-task")
    result = harness.runtime.poll_build(build.build_id, attempt=0, task_id="poll-task")

    assert result == {"build_id": build.build_id, "status": "succeeded"}
    assert jenkins.requests[0].appid == appid
    assert jenkins.requests[0].compose == source
    assert docker_services.calls == [(appid, artifact_compose)]
    assert docker_services.calls[0][1]["volumes"] == source["volumes"]
    records = harness.container.repository.list_services(appid)
    assert len(records) == 1
    assert records[0].appid == appid
    assert records[0].build_id == build.build_id


class OpenConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def public_port_deployment() -> SwarmDeployment:
    return SwarmDeployment(
        appid="demo",
        network_name="orchestrator-demo",
        services=(
            ServiceDeployment(
                service_id="service-demo-web",
                service_name="demo-web",
                image="harbor.example/apps/demo:build-44",
                action="created",
                endpoint="demo-web:18080",
                ports=(DockerPublishedPort(target_port=8080, published_port=18080),),
            ),
        ),
    )


def test_public_port_probe_requires_reachable_published_tcp_port(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = make_harness()
    harness.runtime.settings.public_host = "127.0.0.1"
    harness.runtime.settings.deployment_readiness_timeout_seconds = 0
    monkeypatch.setattr("orchestrator.worker_runtime.socket.create_connection", lambda *_args, **_kwargs: OpenConnection())

    harness.runtime._probe_public_ports(public_port_deployment())

    def refused(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("orchestrator.worker_runtime.socket.create_connection", refused)
    with pytest.raises(DockerServiceError, match="demo-web:18080 is not reachable"):
        harness.runtime._probe_public_ports(public_port_deployment())


def test_failed_jenkins_result_marks_the_build_failed_without_deployment() -> None:
    jenkins = FakeJenkins(
        [JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42, build_number=17)],
        build=JenkinsBuild(number=17, building=False, result="FAILURE"),
        console_log="[Pipeline] { (Checkout)\nfatal: Could not resolve host: git.example\nERROR: script returned exit code 128",
    )
    docker_services = FakeDockerServices(successful_deployment())
    harness = make_harness(jenkins=jenkins, docker_services=docker_services)
    build = queue_build(harness)

    harness.runtime.start_build(build.build_id, task_id="start-task")
    result = harness.runtime.poll_build(build.build_id, attempt=0, task_id="poll-task")

    stored = harness.container.repository.get_build(build.build_id)
    assert result == {"build_id": build.build_id, "status": "failed"}
    assert stored is not None
    assert stored.status is BuildStatus.FAILED
    assert stored.error is not None
    assert "FAILURE" in stored.error
    assert stored.error.startswith("Jenkins build finished with FAILURE；Jenkins #17；")
    assert "阶段=Checkout" in stored.error
    assert "console L2" in stored.error
    assert "DNS 解析失败" in stored.error
    assert "git.example" in stored.error
    assert "退出码=128" in stored.error
    assert jenkins.failure_calls == [17]
    assert jenkins.artifact_calls == []
    assert docker_services.calls == []
    assert harness.container.repository.list_user_images("demo") == []
    assert harness.container.repository.list_services("demo") == []
    assert len(harness.container.repository.list_alerts("demo")) == 1
    assert harness.container.repository.list_alerts("demo")[0].message == stored.error
    assert set(harness.container.repository.list_alerts("demo")[0].model_dump()) == {
        "alert_id", "appid", "build_id", "severity", "message", "resolved_at", "created_at",
    }
    events = harness.container.repository.list_events("demo")
    assert next(event for event in events if event.kind == "build.alert").message == stored.error
    harness.runtime.poll_build(build.build_id, attempt=1, task_id="poll-again")
    assert jenkins.failure_calls == [17]
    assert len(harness.container.repository.list_alerts("demo")) == 1


@pytest.mark.parametrize("console_log", ["", RuntimeError("untrusted transport with password=never-expose")])
def test_jenkins_diagnostic_failure_does_not_hide_original_build_failure(console_log):
    jenkins = FakeJenkins(
        [JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42, build_number=17)],
        build=JenkinsBuild(number=17, building=False, result="FAILURE"),
        console_log=console_log,
    )
    harness = make_harness(jenkins=jenkins)
    build = queue_build(harness)
    harness.runtime.start_build(build.build_id, task_id="start-task")
    harness.runtime.poll_build(build.build_id, attempt=0, task_id="poll-task")
    stored = harness.container.repository.get_build(build.build_id)
    assert stored.status is BuildStatus.FAILED
    assert stored.error.startswith("Jenkins build finished with FAILURE；Jenkins #17；")
    assert "never-expose" not in stored.error
    assert "未识别出具体错误" in stored.error or "错误详情暂不可用" in stored.error


def test_jenkins_failure_redacts_app_environment_settings_and_compose_inputs():
    secrets = ["not-exposed", "configured-gitlab-token", "compose-inline-value", "build-arg-value"]
    jenkins = FakeJenkins(
        [JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42, build_number=17)],
        build=JenkinsBuild(number=17, building=False, result="FAILURE"),
        console_log="[Pipeline] { (Build And Push)\nERROR: request failed " + " ".join(secrets),
    )
    harness = make_harness(jenkins=jenkins)
    harness.runtime.settings.gitlab_token = secrets[1]
    compose = deepcopy(APP_COMPOSE)
    compose["services"]["api"]["environment"] = {"CUSTOM_INPUT": secrets[2]}
    compose["services"]["api"]["build"] = {
        "context": ".",
        "dockerfile": "Dockerfile",
        "args": ["CUSTOM_INPUT=" + secrets[3]],
    }
    build = queue_build(harness, compose=compose)
    harness.container.repository.update_build(build.build_id, {"status": BuildStatus.BUILDING, "jenkins_queue_url": "https://jenkins.example/queue/item/42/", "jenkins_build_number": 17})
    harness.runtime.poll_build(build.build_id, attempt=0, task_id="poll-task")
    stored = harness.container.repository.get_build(build.build_id)
    assert "阶段=Build And Push" in stored.error
    assert all(secret not in stored.error for secret in secrets)
    assert harness.container.repository.list_alerts("demo")[0].message == stored.error


@pytest.mark.parametrize(
    ("artifact", "deployment", "error_fragment", "expect_user_images"),
    [
        (JenkinsArtifactError("Jenkins result artifact is missing"), successful_deployment(), "artifact is missing", False),
        (successful_artifact(), DockerServiceError("Swarm deployment failed"), "Swarm deployment failed", True),
    ],
    ids=["missing-artifact", "deployment-error"],
)
def test_missing_artifact_or_deployment_error_marks_the_build_failed(
    artifact: JenkinsResultArtifact | Exception,
    deployment: SwarmDeployment | Exception,
    error_fragment: str,
    expect_user_images: bool,
) -> None:
    jenkins = FakeJenkins(
        [JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42, build_number=23)],
        build=JenkinsBuild(number=23, building=False, result="SUCCESS"),
        artifact=artifact,
    )
    docker_services = FakeDockerServices(deployment)
    harness = make_harness(jenkins=jenkins, docker_services=docker_services)
    build = queue_build(harness)

    harness.runtime.start_build(build.build_id, task_id="start-task")
    result = harness.runtime.poll_build(build.build_id, attempt=0, task_id="poll-task")

    stored = harness.container.repository.get_build(build.build_id)
    assert result == {"build_id": build.build_id, "status": "failed"}
    assert stored is not None
    assert stored.status is BuildStatus.FAILED
    assert stored.error is not None
    assert error_fragment in stored.error
    assert bool(harness.container.repository.list_user_images("demo")) is expect_user_images
    assert harness.container.repository.list_services("demo") == []


def test_base_image_sync_persists_only_base_images() -> None:
    registry = FakeRegistry()
    harness = make_harness(registry=registry)
    harness.container.repository.save_user_image(
        UserImage(
            image_id="existing-user-image",
            appid="demo",
            build_id="existing-build",
            reference="harbor.example/apps/demo:existing",
        )
    )
    expected_sources = sorted(
        {source for component in harness.container.catalog.components.values() for source in component["images"]}
    )

    result = harness.runtime.sync_base_images(task_id="base-image-task")

    assert result == {"total": len(expected_sources), "synced": len(expected_sources), "failed": 0}
    assert registry.sources == expected_sources
    assert [image.reference for image in harness.container.repository.list_user_images("demo")] == [
        "harbor.example/apps/demo:existing"
    ]
    assert {image.source_image for image in harness.container.repository.list_base_images()} == set(expected_sources)


def test_recovery_requeues_queued_builds_and_restarts_polling_active_builds() -> None:
    harness = make_harness()
    queued = queue_build(harness, "queued")
    active = queue_build(harness, "active")
    for status in (BuildStatus.VALIDATING, BuildStatus.TRIGGERING, BuildStatus.BUILDING):
        harness.container.builds.transition(active.build_id, status)

    result = harness.runtime.recover_builds(task_id="recovery-task")

    assert result == {"start_scheduled": 1, "poll_scheduled": 1}
    assert harness.scheduled_starts == [queued.build_id]
    assert harness.scheduled_polls == [(active.build_id, 0, 7)]


def test_recovery_does_not_add_a_second_poll_chain_while_active_build_is_fresh() -> None:
    harness = make_harness()
    active = queue_build(harness, "active-fresh")
    for status in (BuildStatus.VALIDATING, BuildStatus.TRIGGERING, BuildStatus.BUILDING):
        harness.container.builds.transition(active.build_id, status)
    harness.container.repository.update_build(active.build_id, {"last_polled_at": utc_now()})

    result = harness.runtime.recover_builds(task_id="recovery-task")

    assert result == {"start_scheduled": 0, "poll_scheduled": 0}
    assert harness.scheduled_polls == []


@pytest.mark.parametrize("age_seconds, expected_polls", [(1, 0), (900, 1)])
def test_recovery_normalizes_naive_utc_last_polled_at(
    age_seconds: int, expected_polls: int
) -> None:
    harness = make_harness()
    active = queue_build(harness, "active-naive")
    for status in (BuildStatus.VALIDATING, BuildStatus.TRIGGERING, BuildStatus.BUILDING):
        harness.container.builds.transition(active.build_id, status)
    naive_utc = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).replace(tzinfo=None)
    harness.container.repository.update_build(active.build_id, {"last_polled_at": naive_utc})

    result = harness.runtime.recover_builds(task_id="recovery-task")

    assert result["poll_scheduled"] == expected_polls
    assert len(harness.scheduled_polls) == expected_polls


def test_worker_does_not_mutate_build_after_losing_trigger_lease(monkeypatch):
    jenkins = FakeJenkins([], artifact=successful_artifact())
    harness = make_harness(jenkins=jenkins)
    build = queue_build(harness)
    trigger = jenkins.trigger_build

    def lose_lease(request):
        result = trigger(request)
        harness.container.repository.update_build_resources(build.build_id, {
            "lease_owner": "replacement-worker", "lease_expires_at": utc_now() + timedelta(seconds=60),
        })
        return result

    monkeypatch.setattr(jenkins, "trigger_build", lose_lease)
    assert harness.runtime.start_build(build.build_id, "worker")["status"] == "triggering"
    stored = harness.container.repository.get_build(build.build_id)
    assert stored.error is None and stored.jenkins_queue_url is None
    manifest = harness.container.repository.get_build_resources(build.build_id)
    assert manifest.lease_owner == "replacement-worker" and manifest.jenkins_trigger_pending
    assert not harness.scheduled_polls


def test_cleanup_tombstone_prevents_delayed_deployment_tasks():
    docker = FakeDockerServices(successful_deployment())
    harness = make_harness(docker_services=docker)
    build = queue_build(harness)
    harness.container.repository.update_build_resources(build.build_id, {"cleanup_status": CleanupStatus.SUCCEEDED})
    harness.runtime.start_build(build.build_id, "delayed-start")
    harness.runtime.poll_build(build.build_id, 0, "delayed-poll")
    assert not docker.calls and not harness.scheduled_polls
    assert harness.container.repository.get_build(build.build_id).status == BuildStatus.QUEUED


def test_recovery_and_worker_wait_until_input_snapshot_is_ready(monkeypatch):
    harness = make_harness()
    harness.container.applications.create_app(UserAppCreate(appid="demo", name="Demo", compose=APP_COMPOSE))
    snapshot = harness.container.workspace.snapshot

    def snapshot_while_recovering(appid, build_id):
        assert not harness.container.repository.get_build(build_id).inputs_ready
        assert harness.container.repository.get_build_resources(build_id).lease_owner is not None
        harness.container.repository.update_build(build_id, {"created_at": utc_now() - timedelta(hours=2)})
        assert harness.runtime.start_build(build_id, "too-early")["status"] == "queued"
        assert harness.runtime.recover_builds("recovery") == {"start_scheduled": 0, "poll_scheduled": 0}
        return snapshot(appid, build_id)

    monkeypatch.setattr(harness.container.workspace, "snapshot", snapshot_while_recovering)
    build = harness.container.builds.queue_build("demo", BuildCreate())
    assert build.inputs_ready and not harness.scheduled_starts


@pytest.mark.parametrize("violation", ["owner", "digest", "inventory"])
def test_worker_rejects_invalid_isolated_artifact_contract(violation):
    jenkins = FakeJenkins([JenkinsQueueItem("https://jenkins.example/queue/item/42/", 42, build_number=42)],
        build=JenkinsBuild(42, False, "SUCCESS"), artifact=successful_artifact())
    docker = FakeDockerServices(successful_deployment())
    harness = make_harness(jenkins=jenkins, docker_services=docker)
    build = queue_build(harness)
    assert harness.runtime.start_build(build.build_id, "start")["status"] == "building"
    artifact = jenkins._artifact
    payload = deepcopy(artifact._payload)
    images = artifact.images
    if violation == "owner":
        payload["build_id"] = "f" * 32
    elif violation == "digest":
        payload["image_digests"] = {}
    else:
        reference = f"harbor.example/apps/demo:{build.build_id}-extra"
        images = (*images, reference)
        payload["image_digests"][reference] = "sha256:" + "b" * 64
    jenkins._artifact = JenkinsResultArtifact(images=images, compose=artifact.compose, _payload=payload)
    assert harness.runtime.poll_build(build.build_id, 0, "poll")["status"] == "failed"
    assert not docker.calls


def test_recovery_finishes_interrupted_input_preparation_without_starting_jenkins():
    harness = make_harness()
    build = queue_build(harness)
    harness.container.repository.update_build(build.build_id, {"inputs_ready": False, "created_at": utc_now() - timedelta(hours=2)})
    assert harness.runtime.recover_builds("recovery") == {"start_scheduled": 0, "poll_scheduled": 0}
    assert harness.container.repository.get_build(build.build_id).status == BuildStatus.FAILED
    assert harness.container.build_resources.request_cleanup(build.build_id)[1] == 202


def test_definitively_rejected_trigger_does_not_block_cleanup(monkeypatch):
    jenkins = FakeJenkins([])
    harness = make_harness(jenkins=jenkins)
    build = queue_build(harness)

    def rejected(request):
        raise JenkinsNotStartedError("Jenkins rejected the build trigger (HTTP 403)")

    monkeypatch.setattr(jenkins, "trigger_build", rejected)
    assert harness.runtime.start_build(build.build_id, "worker")["status"] == "failed"
    assert not harness.container.repository.get_build_resources(build.build_id).jenkins_trigger_pending
    assert harness.container.build_resources.request_cleanup(build.build_id)[1] == 202
