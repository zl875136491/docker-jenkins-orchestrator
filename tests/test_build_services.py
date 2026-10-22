import pytest
from cryptography.fernet import Fernet

from orchestrator.config import Settings
from orchestrator.gitlab import GitLabProject, GitLabRepositoryRef
from orchestrator.models import AgentRoleUpdate, BuildCreate, BuildStatus, UserAppCreate, UserAppUpdate
from orchestrator.repository import InMemoryRepository
from orchestrator.route_checks import BUILD_GATE_ERROR, DYNAMIC_ROLE, RouteCheckService, STATIC_ROLE
from orchestrator.secrets import SecretBox
from orchestrator.services import ApplicationService, BuildInputError, BuildService, InvalidBuildTransition
from orchestrator.tasks import InMemoryTaskDispatcher


MAIN_SHA = "a" * 40
RELEASE_SHA = "b" * 40


class ResolvedBuildRepository:
    """Resolve refs only; test checks are explicitly approved in the repository."""

    def __init__(self):
        self.ref_commits = {"main": MAIN_SHA, "release": RELEASE_SHA}
        self.validated = []

    def validate_repository_ref(self, repository_url, git_ref):
        self.validated.append((repository_url, git_ref))
        commit = self.ref_commits.get(git_ref, git_ref)
        return GitLabRepositoryRef(GitLabProject(1, "team/project"), git_ref, commit, repository_url)


def build_service() -> tuple[InMemoryRepository, ApplicationService, BuildService]:
    repository = InMemoryRepository()
    applications = ApplicationService(repository, SecretBox(Fernet.generate_key().decode()))
    dispatcher = InMemoryTaskDispatcher()
    adapter = ResolvedBuildRepository()
    route_checks = RouteCheckService(repository, dispatcher, Settings(environment="test"), gitlab_factory=lambda: adapter)
    applications.route_checks = route_checks
    builds = BuildService(repository, applications, dispatcher, route_checks=route_checks)
    return repository, applications, builds


def approve_static_check(builds: BuildService, appid: str, commit_id: str = MAIN_SHA):
    """Precondition for legacy lifecycle tests, never a production gate bypass."""

    app = builds.application_service.get_record(appid)
    check = builds.route_checks.create_or_reuse(app, commit_id).result
    return builds.repository.update_route_check(check.check_id, {
        "static_status": "succeeded", "static_check_passed": True,
        "probe_payload": {"protocol": "http", "method": "GET", "path": app.require_route,
                          "query_params": {}, "body": None, "assertions": {"expected_status_list": [200]}},
    })


def create_gated_app(applications, *, appid="gated-app", require_route="/health"):
    return applications.create_app(UserAppCreate(
        appid=appid, name="Gated app", repository_url=f"https://git.example/{appid}.git", require_route=require_route,
        compose={"services": {"api": {"image": "example/api"}}},
    ))


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
            require_route="/health",
            repository_url="https://git.example/stateful.git",
            compose={"services": {"api": {"image": "example/api"}}},
        )
    )
    approve_static_check(builds, "stateful")
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
            require_route="/health",
            repository_url="https://git.example/invalid.git",
            compose={"services": {"api": {"image": "example/api"}}},
        )
    )
    approve_static_check(builds, "invalid-transition")
    build = builds.queue_build("invalid-transition", BuildCreate())
    with pytest.raises(InvalidBuildTransition):
        builds.transition(build.build_id, BuildStatus.DEPLOYING)


@pytest.mark.parametrize("status,passed", [
    ("queued", None), ("running", None), ("failed", False), ("succeeded", False), ("failed", True),
])
def test_build_gate_rejects_pending_or_failed_checks_before_creating_any_build(status, passed):
    repository, applications, builds = build_service()
    app = create_gated_app(applications)
    [check] = repository.list_route_checks(app.appid, MAIN_SHA)
    repository.update_route_check(check.check_id, {"static_status": status, "static_check_passed": passed})

    with pytest.raises(BuildInputError, match=BUILD_GATE_ERROR):
        builds.queue_build(app.appid, BuildCreate())

    assert repository.list_builds(appid=app.appid)[1] == 0
    assert repository.build_resources == {}
    assert builds.dispatcher.build_ids == []
    assert not any(event.kind == "build.queued" for event in repository.list_events(app.appid))


@pytest.mark.parametrize("missing", ["route", "check", "check_service"])
def test_build_gate_cannot_be_bypassed_by_omitted_route_or_check_service(missing):
    repository, applications, builds = build_service()
    app = create_gated_app(applications, require_route=None if missing == "route" else "/health")
    if missing == "check":
        assert builds.route_checks.delete_latest(app.appid, MAIN_SHA)
    elif missing == "check_service":
        builds.route_checks = None

    with pytest.raises(BuildInputError, match=BUILD_GATE_ERROR):
        builds.queue_build(app.appid, BuildCreate())

    assert repository.list_builds(appid=app.appid)[1] == 0
    assert builds.dispatcher.build_ids == []


@pytest.mark.parametrize("dimension", ["app", "commit", "route", "role_version"])
def test_passed_cache_for_another_dimension_never_allows_a_build(dimension):
    repository, applications, builds = build_service()
    app = create_gated_app(applications)
    original = approve_static_check(builds, app.appid)
    target_appid = app.appid
    if dimension == "app":
        target_appid = create_gated_app(applications, appid="other-app").appid
    elif dimension == "commit":
        builds.route_checks.gitlab().ref_commits["main"] = RELEASE_SHA
    elif dimension == "route":
        applications.update_app(app.appid, UserAppUpdate(require_route="/changed"))
    else:
        builds.route_checks.roles.update(STATIC_ROLE, AgentRoleUpdate(prompt="A new role version"))

    with pytest.raises(BuildInputError, match=BUILD_GATE_ERROR):
        builds.queue_build(target_appid, BuildCreate())

    assert repository.get_route_check(original.check_id).static_check_passed is True
    assert repository.list_builds()[1] == 0
    assert builds.dispatcher.build_ids == []


def test_build_gate_resolves_requested_ref_and_requires_that_exact_commit():
    repository, applications, builds = build_service()
    app = create_gated_app(applications)
    approve_static_check(builds, app.appid)

    with pytest.raises(BuildInputError, match=BUILD_GATE_ERROR):
        builds.queue_build(app.appid, BuildCreate(git_ref="release"))
    release_check = approve_static_check(builds, app.appid, RELEASE_SHA)
    build = builds.queue_build(app.appid, BuildCreate(git_ref="release"))

    assert build.git_ref == "release"
    assert build.git_commit_sha == RELEASE_SHA
    assert build.route_check_id == release_check.check_id
    assert builds.route_checks.gitlab().validated[-1] == (app.repository_url, "release")
    assert repository.list_builds(appid=app.appid)[1] == 1


def test_queued_build_snapshots_passed_check_and_latest_dynamic_role_without_aliases():
    repository, applications, builds = build_service()
    app = create_gated_app(applications)
    checked = approve_static_check(builds, app.appid)
    dynamic_role = builds.route_checks.roles.update(DYNAMIC_ROLE, AgentRoleUpdate(prompt="Latest dynamic debugger"))

    build = builds.queue_build(app.appid, BuildCreate())

    assert build.inputs_ready is True
    assert build.git_commit_sha == MAIN_SHA
    assert build.route_check_id == checked.check_id
    assert build.route_check_generation == checked.generation
    assert build.require_route_snapshot == "/health"
    assert build.static_agent_role_version == checked.static_agent_role_version
    assert build.dynamic_agent_role_version == dynamic_role.agent_role_version == 2
    assert build.route_dynamic_role_snapshot["prompt"] == "Latest dynamic debugger"
    assert build.route_probe_payload == checked.probe_payload
    assert build.static_check_passed is True
    assert build.celery_task_id
    assert builds.dispatcher.build_ids == [build.build_id]
    assert repository.get_build_resources(build.build_id).appid == app.appid
    assert builds.route_checks.verify_build_gate(build, applications.get_record(app.appid)).check_id == checked.check_id

    checked.probe_payload["assertions"]["expected_status_list"].append(500)
    repository.update_route_check(checked.check_id, {"probe_payload": checked.probe_payload})
    repository.update_app(app.appid, {"compose": {"services": {"changed": {"image": "other"}}}})
    builds.route_checks.roles.update(DYNAMIC_ROLE, AgentRoleUpdate(prompt="Another dynamic debugger"))
    persisted = repository.get_build(build.build_id)
    assert persisted.route_probe_payload["assertions"]["expected_status_list"] == [200]
    assert persisted.compose_snapshot == {"services": {"api": {"image": "example/api"}}}
    assert persisted.route_dynamic_role_snapshot["prompt"] == "Latest dynamic debugger"
