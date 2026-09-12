import sys
from types import SimpleNamespace

import pytest

from orchestrator.registry import DockerRegistrySynchronizer, RegistrySyncError


class FakeImage:
    def __init__(self) -> None:
        self.tags: list[tuple[str, str | None]] = []
        self.attrs = {}

    def tag(self, repository: str, tag: str | None = None) -> bool:
        self.tags.append((repository, tag))
        return True


class FakeImages:
    def __init__(self, events) -> None:
        self.events = events
        self.image = FakeImage()
        self.pull_calls: list[str] = []
        self.push_calls: list[tuple[str, str | None, bool, bool]] = []

    def pull(self, reference: str) -> FakeImage:
        self.pull_calls.append(reference)
        return self.image

    def push(self, repository: str, tag: str | None = None, stream: bool = False, decode: bool = False):
        self.push_calls.append((repository, tag, stream, decode))
        return iter(self.events)


class FakeDocker:
    def __init__(self, events) -> None:
        self.images = FakeImages(events)
        self.login_calls: list[dict[str, str | None]] = []

    def login(self, **kwargs) -> None:
        self.login_calls.append(kwargs)


def test_registry_sync_pulls_tags_pushes_and_returns_harbor_reference() -> None:
    docker = FakeDocker([{"status": "Pushed"}, {"aux": {"Digest": "sha256:abcdef123456"}}])
    synchronizer = DockerRegistrySynchronizer(
        "https://harbor.example/",
        "robot$orchestrator",
        "secret-value",
        docker_client=docker,
    )

    result = synchronizer.sync("python:3.12")

    assert result.source_image == "python:3.12"
    assert result.harbor_reference == "harbor.example/boot-images/python:3.12"
    assert result.digest == "sha256:abcdef123456"
    assert result.status == "synced"
    assert result.synced_at is not None
    assert docker.login_calls == [
        {"username": "robot$orchestrator", "password": "secret-value", "registry": "harbor.example"}
    ]
    assert docker.images.pull_calls == ["python:3.12"]
    assert docker.images.image.tags == [("harbor.example/boot-images/python", "3.12")]
    assert docker.images.push_calls == [("harbor.example/boot-images/python", "3.12", True, True)]
    assert "secret-value" not in repr(synchronizer)


def test_registry_sync_rejects_latest_before_contacting_docker() -> None:
    docker = FakeDocker([])
    synchronizer = DockerRegistrySynchronizer("harbor.example", docker_client=docker)

    with pytest.raises(RegistrySyncError, match="stable tag"):
        synchronizer.sync("redis:latest")

    assert docker.images.pull_calls == []
    assert docker.login_calls == []


def test_registry_sync_hides_registry_error_details_and_sync_many_keeps_failure_record() -> None:
    docker = FakeDocker([{"error": "denied: password=secret-value"}])
    synchronizer = DockerRegistrySynchronizer("harbor.example", "user", "secret-value", docker_client=docker)

    with pytest.raises(RegistrySyncError) as raised:
        synchronizer.sync("redis:7.4")
    assert "secret-value" not in str(raised.value)

    results = synchronizer.sync_many(["redis:latest"])
    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].digest is None
    assert results[0].error == "Base image must use an explicit stable tag"


def test_registry_uses_docker_environment_defaults_without_an_explicit_endpoint(monkeypatch) -> None:
    expected_client = object()
    docker_module = SimpleNamespace(
        from_env=lambda: expected_client,
        DockerClient=lambda **_: pytest.fail("DockerClient should not be called without an endpoint"),
    )
    monkeypatch.setitem(sys.modules, "docker", docker_module)

    assert DockerRegistrySynchronizer._create_client(None) is expected_client
