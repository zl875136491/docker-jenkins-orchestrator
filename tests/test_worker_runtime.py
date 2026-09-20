from __future__ import annotations

from dataclasses import dataclass
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
)
from orchestrator.models import BaseImage, BuildCreate, BuildStatus, UserAppCreate, UserImage, utc_now
from orchestrator.worker_runtime import WorkerRuntime


APP_COMPOSE = {
    "version": "3.9",
    "services": {
        "api": {
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
    ) -> None:
        self._queue_items = list(queue_items)
        self._build = build
        self._artifact = artifact
        self.requests: list[JenkinsBuildRequest] = []
        self.queue_calls: list[str] = []
        self.build_calls: list[int] = []
        self.artifact_calls: list[int] = []

    def trigger_build(self, request: JenkinsBuildRequest) -> JenkinsQueueItem:
        self.requests.append(request)
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


def queue_build(harness: RuntimeHarness, appid: str = "demo"):
    harness.container.applications.create_app(
        UserAppCreate(
            appid=appid,
            name="Demo",
            repository_url=f"https://git.example/{appid}.git",
            git_ref="main",
            environment={"DEPLOY_TOKEN": "not-exposed"},
            compose=APP_COMPOSE,
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
    assert stored.images == ["harbor.example/apps/demo:build-42"]
    assert stored.service_ids == ["service-demo-api"]
    assert harness.scheduled_polls == [(build.build_id, 0, 7), (build.build_id, 1, 7)]
    assert jenkins.queue_calls == ["https://jenkins.example/queue/item/42/"] * 2
    assert jenkins.build_calls == [91]
    assert jenkins.artifact_calls == [91]
    assert [image.reference for image in harness.container.repository.list_user_images("demo")] == [
        "harbor.example/apps/demo:build-42"
    ]
    assert [service.service_id for service in harness.container.repository.list_services("demo")] == ["service-demo-api"]
    stored_service = harness.container.repository.list_services("demo")[0]
    assert [port.model_dump() for port in stored_service.published_ports] == [
        {"target_port": 8080, "published_port": 18080, "protocol": "tcp", "mode": "ingress"}
    ]
    assert docker_services.calls == [("demo", artifact.compose)]


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
    assert jenkins.artifact_calls == []
    assert docker_services.calls == []
    assert harness.container.repository.list_user_images("demo") == []
    assert harness.container.repository.list_services("demo") == []
    assert len(harness.container.repository.list_alerts("demo")) == 1


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
