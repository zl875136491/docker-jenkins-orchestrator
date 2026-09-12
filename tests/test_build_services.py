import pytest
from cryptography.fernet import Fernet

from orchestrator.models import BuildCreate, BuildStatus, UserAppCreate
from orchestrator.repository import InMemoryRepository
from orchestrator.secrets import SecretBox
from orchestrator.services import ApplicationService, BuildService, InvalidBuildTransition
from orchestrator.tasks import InMemoryTaskDispatcher


def build_service() -> tuple[InMemoryRepository, ApplicationService, BuildService]:
    repository = InMemoryRepository()
    applications = ApplicationService(repository, SecretBox(Fernet.generate_key().decode()))
    builds = BuildService(repository, applications, InMemoryTaskDispatcher())
    return repository, applications, builds


def test_environment_is_encrypted_before_persistence() -> None:
    repository, applications, _ = build_service()
    applications.create_app(
        UserAppCreate(
            appid="encrypted",
            name="Encrypted",
            repository_url="https://git.example/encrypted.git",
            environment={"TOKEN": "never-store-this-plain"},
            compose={"services": {"api": {"image": "example/api"}}},
        )
    )

    stored = repository.get_app("encrypted")
    assert stored is not None
    assert "never-store-this-plain" not in stored.environment_ciphertext
    assert applications.get_environment("encrypted") == {"TOKEN": "never-store-this-plain"}


def test_build_state_machine_records_events_and_alerts() -> None:
    repository, applications, builds = build_service()
    applications.create_app(
        UserAppCreate(
            appid="stateful",
            name="Stateful",
            repository_url="https://git.example/stateful.git",
            compose={"services": {"api": {"image": "example/api"}}},
        )
    )
    build = builds.queue_build("stateful", BuildCreate())
    assert build.status == BuildStatus.QUEUED
    assert build.celery_task_id

    for target in (BuildStatus.VALIDATING, BuildStatus.TRIGGERING, BuildStatus.BUILDING):
        build = builds.transition(build.build_id, target)
    assert build.status == BuildStatus.BUILDING
    failed = builds.fail(build.build_id, "Jenkins reported a failure")
    assert failed.status == BuildStatus.FAILED
    assert repository.list_alerts("stateful")[0].message == "Jenkins reported a failure"
    assert {event.kind for event in repository.list_events("stateful")} >= {"build.queued", "build.failed"}


def test_build_state_machine_rejects_skipped_steps() -> None:
    _, applications, builds = build_service()
    applications.create_app(
        UserAppCreate(
            appid="invalid-transition",
            name="Invalid",
            repository_url="https://git.example/invalid.git",
            compose={"services": {"api": {"image": "example/api"}}},
        )
    )
    build = builds.queue_build("invalid-transition", BuildCreate())
    with pytest.raises(InvalidBuildTransition):
        builds.transition(build.build_id, BuildStatus.DEPLOYING)
