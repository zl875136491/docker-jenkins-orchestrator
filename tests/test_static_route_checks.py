from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from orchestrator.agent_executor import CodexCliExecutor
from orchestrator.config import Settings
from orchestrator.gitlab import GitLabHttpAdapter, GitLabProject, GitLabRepositoryRef
from orchestrator.models import AgentRoleUpdate, BuildJob, UserAppRecord, utc_now
from orchestrator.repository import InMemoryRepository, MongoRepository
from orchestrator.route_checks import (
    AgentRoleService, BUILD_GATE_ERROR, DYNAMIC_ROLE, FAILED_CACHE_MESSAGE,
    RouteCheckInputError, RouteCheckService, RouteCheckUnavailableError,
    STATIC_ROLE, SUCCESS_CACHE_MESSAGE,
)
from orchestrator.tasks import InMemoryTaskDispatcher


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Route check test",
         "-c", "user.email=route-check@example.invalid", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _probe(route="/health"):
    return {"protocol": "http", "method": "GET", "path": route, "query_params": {}, "body": None,
            "assertions": {"expected_status_list": [200]}}


def _output(**fields):
    output = {"route_found": True, "probe_payload": _probe(), "patch_available": False,
              "diff_list": [], "report_text": ""}
    output.update(fields)
    return output


class FakeAgent:
    def __init__(self, output=None, *, action=None, error=None):
        self.output = _output() if output is None else output
        self.action = action
        self.error = error
        self.calls = []

    def run_json(self, role, context, *, cwd):
        root = Path(cwd)
        self.calls.append({"role": role.model_copy(deep=True), "context": deepcopy(context), "cwd": root,
                           "head": _git(root, "rev-parse", "HEAD"),
                           "source": (root / "app.py").read_text(encoding="utf-8")})
        if self.action is not None:
            self.action(root)
        if self.error is not None:
            raise self.error
        return deepcopy(self.output)


class LocalGitAdapter(GitLabHttpAdapter):
    """Use the production full-clone/checkout implementation without a server."""

    def __init__(self, source):
        super().__init__("https://git.example", "private-test-token")
        self.source = source
        self.validated = []
        self.checked_out = []
        self.resolve_override = None
        self.checkout_override = None

    def validate_repository_ref(self, repository_url, git_ref):
        self.validated.append((repository_url, git_ref))
        commit = self.resolve_override or _git(self.source, "rev-parse", "--verify", f"{git_ref}^{{commit}}")
        return GitLabRepositoryRef(GitLabProject(1, "team/project"), git_ref, commit, repository_url)

    def _clone_url(self, ref):
        return self.source.as_uri()

    def checkout_commit(self, ref, destination):
        self.checked_out.append(ref.commit_sha)
        if self.checkout_override is not None:
            ref = replace(ref, commit_sha=self.checkout_override)
        return super().checkout_commit(ref, destination)


@pytest.fixture(params=["memory", "mongo"])
def repository(request):
    if request.param == "memory":
        repository = InMemoryRepository()
    else:
        mongomock = pytest.importorskip("mongomock")
        client = mongomock.MongoClient(tz_aware=True)
        repository = MongoRepository(client.static_check_tests, client=client)
    repository.ensure_indexes()
    yield repository
    repository.close()


@pytest.fixture
def environment(tmp_path, repository):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--quiet", "--initial-branch=main")
    original = "def health():\n    return {'status': 'ok'}\n"
    (source / "app.py").write_text(original, encoding="utf-8")
    _git(source, "add", "--all")
    _git(source, "commit", "--quiet", "-m", "Initial route")
    commit = _git(source, "rev-parse", "HEAD")
    adapter = LocalGitAdapter(source)
    dispatcher = InMemoryTaskDispatcher()
    agent = FakeAgent()
    settings = Settings(environment="test", workspace_root=tmp_path / "workspaces")
    service = RouteCheckService(repository, dispatcher, settings, executor=agent, gitlab_factory=lambda: adapter)
    app = repository.create_app(UserAppRecord(
        appid="route-app", name="Route app", repository_url="https://git.example/team/project.git",
        git_ref="main", require_route="/health", environment_ciphertext="{}",
    ))
    return SimpleNamespace(source=source, original=original, commit=commit, adapter=adapter,
                           dispatcher=dispatcher, agent=agent, settings=settings, service=service,
                           app=app, repository=repository)


def _complete(env, result, passed=True):
    return env.repository.update_route_check(result.check_id, {
        "static_status": "succeeded" if passed else "failed", "static_check_passed": passed,
        "probe_payload": _probe() if passed else None,
        "static_report_text": None if passed else "目标路由缺失",
    })


def _build(env, check, **fields):
    values = dict(build_id="build-1", appid=env.app.appid, git_ref=env.app.git_ref,
                  git_commit_sha=check.commit_id, route_check_id=check.check_id,
                  route_check_generation=check.generation, require_route_snapshot=check.require_route,
                  static_agent_role_version=check.static_agent_role_version,
                  dynamic_agent_role_version=check.dynamic_agent_role_version, static_check_passed=True)
    values.update(fields)
    return BuildJob(**values)


def test_builtin_roles_defaults_and_restart_preserve_configuration(repository):
    roles = AgentRoleService(repository)
    assert {role.role_key for role in roles.list()} == {STATIC_ROLE, DYNAMIC_ROLE}
    for role in roles.list():
        assert role.prompt
        assert role.output_schema["type"] == "object"
        assert role.runtime_config.authorization_mode == "never"
        assert role.runtime_config.sandbox_mode == "danger-full-access"
        assert role.runtime_config.skip_git_repo_check is True
        assert role.agent_role_version == 1
    changed = roles.update(STATIC_ROLE, AgentRoleUpdate(prompt="A configured prompt"))
    restarted = AgentRoleService(repository)
    assert restarted.get(STATIC_ROLE) == changed
    with pytest.raises(KeyError, match="Agent role not found"):
        restarted.get("unknown")


@pytest.mark.parametrize("field", ["prompt", "output_schema", "runtime_config"])
def test_any_role_update_including_same_value_increments_version(repository, field):
    roles = AgentRoleService(repository)
    original = roles.get(STATIC_ROLE)
    value = original.model_dump()[field]
    first = roles.update(STATIC_ROLE, AgentRoleUpdate(**{field: value}))
    second = roles.update(STATIC_ROLE, AgentRoleUpdate(**{field: value}))
    assert first.agent_role_version == 2
    assert second.agent_role_version == 3
    assert second.model_dump()[field] == value
    assert roles.get(DYNAMIC_ROLE).agent_role_version == 1


def test_partial_runtime_update_merges_only_explicit_fields(repository):
    roles = AgentRoleService(repository)
    first = roles.update(STATIC_ROLE, AgentRoleUpdate(runtime_config={
        "authorization_mode": "on-request", "sandbox_mode": "read-only", "skip_git_repo_check": False,
        "model": "configured-model", "reasoning_effort": "high", "timeout_seconds": 30,
    }))
    changed = roles.update(STATIC_ROLE, AgentRoleUpdate(runtime_config={"timeout_seconds": 45}))
    assert changed.runtime_config.model_dump() == {**first.runtime_config.model_dump(), "timeout_seconds": 45}
    assert changed.agent_role_version == 3


@pytest.mark.parametrize("schema", [
    {"type": "unknown"}, {"type": "array"}, {"properties": {"result": {"type": 42}}},
])
def test_invalid_role_schema_does_not_mutate_the_saved_version(repository, schema):
    roles = AgentRoleService(repository)
    original = roles.get(STATIC_ROLE)
    with pytest.raises(RouteCheckInputError, match="JSON"):
        roles.update(STATIC_ROLE, AgentRoleUpdate(output_schema=schema))
    assert roles.get(STATIC_ROLE) == original


@pytest.mark.parametrize("update", [{}, {"prompt": None}, {"output_schema": None}, {"runtime_config": None},
                                  {"prompt": ""}, {"runtime_config": {"unknown": True}}])
def test_empty_null_or_invalid_role_updates_are_rejected(repository, update):
    roles = AgentRoleService(repository)
    original = roles.get(STATIC_ROLE)
    with pytest.raises(ValidationError):
        roles.update(STATIC_ROLE, AgentRoleUpdate(**update))
    assert roles.get(STATIC_ROLE) == original


def test_concurrent_role_updates_increment_once_per_successful_request(repository):
    roles = AgentRoleService(repository)
    barrier = Barrier(8)

    def update(index):
        barrier.wait(timeout=10)
        return roles.update(STATIC_ROLE, AgentRoleUpdate(prompt=f"Concurrent prompt {index}"))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(update, range(8)))
    assert sorted(role.agent_role_version for role in results) == list(range(2, 10))
    assert roles.get(STATIC_ROLE).agent_role_version == 9


def test_submit_resolves_latest_branch_instead_of_persisted_compose_commit(environment):
    env = environment
    env.app.compose_source_commit = env.commit
    (env.source / "app.py").write_text(env.original + "# newest branch commit\n", encoding="utf-8")
    _git(env.source, "add", "--all")
    _git(env.source, "commit", "--quiet", "-m", "Advance branch")
    latest = _git(env.source, "rev-parse", "HEAD")

    submitted = env.service.schedule_for_app(env.app)

    assert latest != env.commit
    assert submitted.result.commit_id == latest
    assert env.adapter.validated == [(env.app.repository_url, "main")]
    assert env.dispatcher.static_check_generations == [(submitted.check_id, 1)]
    assert submitted.result.static_role_snapshot["agent_role_version"] == 1
    assert submitted.result.dynamic_role_snapshot["agent_role_version"] == 1


def test_submit_explicit_commit_resolves_and_verifies_the_requested_sha(environment):
    env = environment
    submitted = env.service.submit(env.app, commit_id=env.commit)
    assert submitted.result.commit_id == env.commit
    assert env.adapter.validated == [(env.app.repository_url, env.commit)]
    env.adapter.resolve_override = "b" * 40
    with pytest.raises(RouteCheckInputError, match="different commit"):
        env.service.submit(env.app, commit_id=env.commit)
    assert len(env.dispatcher.static_check_ids) == 1


def test_auto_schedule_without_route_is_noop_but_manual_submit_is_rejected(environment):
    env = environment
    app = env.app.model_copy(update={"require_route": None})
    assert env.service.schedule_for_app(app) is None
    with pytest.raises(RouteCheckInputError, match="require_route"):
        env.service.submit(app)
    assert env.dispatcher.static_check_ids == []
    assert env.adapter.validated == []


@pytest.mark.parametrize("commit", ["main", "a" * 7, "a" * 39, "g" * 40])
def test_checks_require_full_immutable_commit_ids(environment, commit):
    env = environment
    with pytest.raises(RouteCheckInputError, match="immutable"):
        env.service.create_or_reuse(env.app, commit)
    assert env.dispatcher.static_check_ids == []


@pytest.mark.parametrize("passed,message", [(True, SUCCESS_CACHE_MESSAGE), (False, FAILED_CACHE_MESSAGE)])
def test_automatic_submissions_reuse_success_and_failure_cache_with_exact_chinese_messages(environment, passed, message):
    env = environment
    original = env.service.create_or_reuse(env.app, env.commit)
    completed = _complete(env, original.result, passed)

    cached = env.service.schedule_for_app(env.app)

    assert cached.cache_hit is True
    assert cached.queued is False
    assert cached.message == message
    assert cached.result == completed
    assert cached.check_id == original.check_id
    assert cached.task_id == original.task_id
    assert len(env.dispatcher.static_check_ids) == 1


def test_pending_duplicate_submission_reuses_the_same_message_and_task(environment):
    env = environment
    original = env.service.create_or_reuse(env.app, env.commit)
    duplicate = env.service.create_or_reuse(env.app, env.commit)
    assert duplicate.check_id == original.check_id
    assert duplicate.task_id == original.task_id
    assert duplicate.cache_hit is False
    assert duplicate.queued is True
    assert duplicate.message == "静态检查任务已提交"
    assert len(env.dispatcher.static_check_ids) == 1


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed"])
def test_force_ignores_terminal_and_pending_cache_and_supersedes_generation(environment, status):
    env = environment
    original = env.service.create_or_reuse(env.app, env.commit)
    env.repository.update_route_check(original.check_id, {
        "static_status": status, "static_run_token": "old-worker", "static_lease_expires_at": utc_now() + timedelta(minutes=5),
        "static_check_passed": True if status == "succeeded" else False if status == "failed" else None,
        "dynamic_status": "failed", "dynamic_check_passed": False, "probe_payload": _probe(),
        "probe_result": {"error": "old"}, "diff_list": ["old diff"], "static_report_text": "old static report",
        "dynamic_report_text": "old dynamic report",
    })

    forced = env.service.create_or_reuse(env.app, env.commit, force=True)

    assert forced.check_id == original.check_id
    assert forced.result.id == original.result.id
    assert forced.result.generation == 2
    assert forced.cache_hit is False
    assert forced.queued is True
    assert forced.task_id != original.task_id
    for field in ("static_check_passed", "dynamic_check_passed", "static_run_token", "static_lease_expires_at",
                  "probe_payload", "probe_result", "static_report_text", "dynamic_report_text"):
        assert getattr(forced.result, field) is None
    assert forced.result.static_status == "queued"
    assert forced.result.dynamic_status == "pending"
    assert forced.result.diff_list == []
    assert env.dispatcher.static_check_generations == [(original.check_id, 1), (original.check_id, 2)]
    assert env.service.execute_static(original.check_id, 1)["status"] == "superseded"
    assert env.agent.calls == []


def test_role_or_route_changes_get_distinct_four_dimensional_cache_entries(environment):
    env = environment
    first = env.service.create_or_reuse(env.app, env.commit)
    _complete(env, first.result)
    env.service.roles.update(STATIC_ROLE, AgentRoleUpdate(prompt=env.service.roles.get(STATIC_ROLE).prompt))
    second = env.service.create_or_reuse(env.app, env.commit)
    changed_app = env.app.model_copy(update={"require_route": "/changed"})
    third = env.service.create_or_reuse(changed_app, env.commit)
    assert len({first.check_id, second.check_id, third.check_id}) == 3
    assert second.result.static_agent_role_version == 2
    assert third.result.require_route == "/changed"
    assert len(env.service.results(env.app.appid, env.commit)) == 3


def test_delete_uses_latest_app_route_and_role_version_and_keeps_all_other_history(environment):
    env = environment
    first = env.service.create_or_reuse(env.app, env.commit)
    env.service.roles.update(STATIC_ROLE, AgentRoleUpdate(prompt="Latest role"))
    second = env.service.create_or_reuse(env.app, env.commit)
    env.app = env.repository.update_app(env.app.appid, {"require_route": "/changed"})
    third = env.service.create_or_reuse(env.app, env.commit)

    assert env.service.delete_latest(env.app.appid, env.commit) is True
    assert env.service.delete_latest(env.app.appid, env.commit) is False
    assert env.repository.get_route_check(third.check_id) is None
    assert env.repository.get_route_check(first.check_id) is not None
    assert env.repository.get_route_check(second.check_id) is not None
    with pytest.raises(KeyError, match="App not found"):
        env.service.delete_latest("missing", env.commit)
    with pytest.raises(KeyError, match="App not found"):
        env.service.results("missing", env.commit)
    env.repository.update_app(env.app.appid, {"require_route": None})
    assert env.service.delete_latest(env.app.appid, env.commit) is False


def test_static_gate_accepts_only_a_passed_current_check(environment):
    env = environment
    result = _complete(env, env.service.create_or_reuse(env.app, env.commit).result)
    assert env.service.require_static_pass(env.app, env.commit) == result
    assert env.service.require_static_pass(env.app, env.commit, generation=1) == result
    assert env.service.verify_build_gate(_build(env, result), env.app) == result


@pytest.mark.parametrize("dimension", ["app_id", "commit_id", "require_route", "static_agent_role_version", "generation", "missing_route"])
def test_static_gate_checks_every_cache_dimension_and_generation(environment, dimension):
    env = environment
    _complete(env, env.service.create_or_reuse(env.app, env.commit).result)
    app, commit, generation = env.app, env.commit, 1
    if dimension == "app_id":
        app = app.model_copy(update={"appid": "another-app"})
    elif dimension == "commit_id":
        commit = "b" * 40
    elif dimension == "require_route":
        app = app.model_copy(update={"require_route": "/other"})
    elif dimension == "static_agent_role_version":
        env.service.roles.update(STATIC_ROLE, AgentRoleUpdate(prompt="A new version"))
    elif dimension == "generation":
        generation = 2
    else:
        app = app.model_copy(update={"require_route": None})
    with pytest.raises(RouteCheckInputError, match=BUILD_GATE_ERROR):
        env.service.require_static_pass(app, commit, generation=generation)


@pytest.mark.parametrize("status,passed", [("queued", None), ("running", True), ("failed", True), ("succeeded", False)])
def test_static_gate_rejects_any_nonterminal_or_nonpassed_result(environment, status, passed):
    env = environment
    result = env.service.create_or_reuse(env.app, env.commit).result
    env.repository.update_route_check(result.check_id, {"static_status": status, "static_check_passed": passed})
    with pytest.raises(RouteCheckInputError, match=BUILD_GATE_ERROR):
        env.service.require_static_pass(env.app, env.commit)


@pytest.mark.parametrize("fields", [
    {"git_commit_sha": None}, {"git_commit_sha": "b" * 40}, {"require_route_snapshot": "/other"},
    {"static_check_passed": False}, {"route_check_id": None}, {"route_check_id": "different-check"},
    {"static_agent_role_version": 2}, {"route_check_generation": 2},
])
def test_build_gate_rejects_stale_or_incomplete_snapshots(environment, fields):
    env = environment
    result = _complete(env, env.service.create_or_reuse(env.app, env.commit).result)
    with pytest.raises(RouteCheckInputError, match=BUILD_GATE_ERROR):
        env.service.verify_build_gate(_build(env, result, **fields), env.app)


@pytest.mark.parametrize("fields", [{"appid": "another-app"}, {"route_check_generation": None}])
def test_build_gate_rejects_wrong_app_or_missing_generation(environment, fields):
    env = environment
    result = _complete(env, env.service.create_or_reuse(env.app, env.commit).result)
    with pytest.raises(RouteCheckInputError, match=BUILD_GATE_ERROR):
        env.service.verify_build_gate(_build(env, result, **fields), env.app)


def _assert_original_and_temporary_worktrees_are_clean(env):
    assert (env.source / "app.py").read_text(encoding="utf-8") == env.original
    assert _git(env.source, "status", "--porcelain") == ""
    assert _git(env.source, "rev-parse", "HEAD") == env.commit
    temporary_root = env.settings.workspace_root / "route-checks"
    assert not temporary_root.exists() or list(temporary_root.iterdir()) == []


def test_static_case_a_saves_valid_probe_and_uses_real_immutable_temporary_checkout(environment):
    env = environment
    submitted = env.service.submit(env.app)

    outcome = env.service.execute_static(submitted.check_id, submitted.result.generation)

    result = env.repository.get_route_check(submitted.check_id)
    assert outcome == {"check_id": submitted.check_id, "status": "succeeded"}
    assert result.static_check_passed is True
    assert result.probe_payload == {**_probe(), "headers": {}}
    assert result.static_report_text is None
    assert result.diff_list == []
    assert result.static_run_token is None
    assert result.static_lease_expires_at is None
    assert env.adapter.checked_out == [env.commit]
    assert env.adapter.validated[-1] == (env.app.repository_url, env.commit)
    assert len(env.agent.calls) == 1
    call = env.agent.calls[0]
    assert call["head"] == env.commit
    assert call["source"] == env.original
    assert call["cwd"] != env.source
    assert call["context"] == {"app_id": env.app.appid, "commit_id": env.commit,
                                "require_route": "/health", "task": "static_route_check"}
    assert not call["cwd"].exists()
    assert env.service.require_static_pass(env.app, env.commit) == result
    _assert_original_and_temporary_worktrees_are_clean(env)


def test_static_case_b_keeps_reviewable_patch_without_approving_or_mutating_original(environment):
    env = environment
    patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
    env.agent.output = _output(route_found=False, patch_available=True, diff_list=[patch], report_text="需要补充目标路由")
    submitted = env.service.create_or_reuse(env.app, env.commit)

    outcome = env.service.execute_static(submitted.check_id, 1)

    result = env.repository.get_route_check(submitted.check_id)
    assert outcome["status"] == "failed"
    assert result.static_check_passed is False
    assert result.diff_list == [patch]
    assert result.probe_payload is None
    assert result.static_report_text == "需要补充目标路由"
    with pytest.raises(RouteCheckInputError, match=BUILD_GATE_ERROR):
        env.service.require_static_pass(env.app, env.commit)
    _assert_original_and_temporary_worktrees_are_clean(env)


def test_static_case_c_saves_concise_diagnostic_when_route_cannot_be_repaired(environment):
    env = environment
    env.agent.output = _output(route_found=False, patch_available=False, report_text="未定义 /health；缺少对应处理函数")
    submitted = env.service.create_or_reuse(env.app, env.commit)

    assert env.service.execute_static(submitted.check_id, 1)["status"] == "failed"
    result = env.repository.get_route_check(submitted.check_id)
    assert result.static_check_passed is False
    assert result.diff_list == []
    assert result.probe_payload is None
    assert result.static_report_text == "未定义 /health；缺少对应处理函数"
    _assert_original_and_temporary_worktrees_are_clean(env)


@pytest.mark.parametrize("change", ["tracked", "staged", "untracked", "committed", "empty_commit"])
def test_actual_agent_edits_or_commits_never_approve_the_original_commit(environment, change):
    env = environment

    def edit(root):
        if change == "empty_commit":
            _git(root, "commit", "--quiet", "--allow-empty", "-m", "Agent changed HEAD")
            return
        path = root / ("extra_route.py" if change == "untracked" else "app.py")
        path.write_text("def generated_route():\n    return 'repaired'\n", encoding="utf-8")
        if change in {"staged", "committed"}:
            _git(root, "add", "--all")
        if change == "committed":
            _git(root, "commit", "--quiet", "-m", "Agent temporary patch")

    env.agent.action = edit
    # Deliberately claim success and no diff. Source evidence must take priority.
    env.agent.output = _output()
    submitted = env.service.create_or_reuse(env.app, env.commit)

    assert env.service.execute_static(submitted.check_id, 1)["status"] == "failed"
    result = env.repository.get_route_check(submitted.check_id)
    assert result.static_check_passed is False
    assert result.probe_payload is None
    assert result.static_report_text
    if change != "empty_commit":
        assert len(result.diff_list) == 1
        assert "diff --git" in result.diff_list[0]
        assert "+def generated_route():" in result.diff_list[0]
    _assert_original_and_temporary_worktrees_are_clean(env)


def test_queued_check_uses_role_snapshot_even_after_live_configuration_changes(environment):
    env = environment
    original_role = env.service.roles.get(STATIC_ROLE)
    submitted = env.service.create_or_reuse(env.app, env.commit)
    env.service.roles.update(STATIC_ROLE, AgentRoleUpdate(prompt="Changed after submission"))
    env.service.roles.update(DYNAMIC_ROLE, AgentRoleUpdate(prompt="New dynamic prompt"))

    assert env.service.execute_static(submitted.check_id, 1)["status"] == "succeeded"
    role_used = env.agent.calls[0]["role"]
    assert role_used.agent_role_version == 1
    assert role_used.prompt == original_role.prompt
    result = env.repository.get_route_check(submitted.check_id)
    assert result.dynamic_agent_role_version == 1
    assert result.dynamic_role_snapshot["agent_role_version"] == 1
    with pytest.raises(RouteCheckInputError, match=BUILD_GATE_ERROR):
        env.service.require_static_pass(env.app, env.commit)


def test_advancing_branch_after_submit_cannot_change_the_checked_commit(environment):
    env = environment
    submitted = env.service.create_or_reuse(env.app, env.commit)
    newer_source = env.original + "# Added after scheduling\n"
    (env.source / "app.py").write_text(newer_source, encoding="utf-8")
    _git(env.source, "add", "--all")
    _git(env.source, "commit", "--quiet", "-m", "Advance branch after queueing")
    latest = _git(env.source, "rev-parse", "HEAD")

    assert env.service.execute_static(submitted.check_id, 1)["status"] == "succeeded"
    assert latest != env.commit
    assert env.agent.calls[0]["head"] == env.commit
    assert env.agent.calls[0]["source"] == env.original
    assert (env.source / "app.py").read_text(encoding="utf-8") == newer_source
    assert _git(env.source, "rev-parse", "HEAD") == latest
    assert _git(env.source, "status", "--porcelain") == ""


@pytest.mark.parametrize("mismatch", ["resolved_ref", "checkout_head"])
def test_incorrect_resolver_or_checkout_cannot_silently_check_a_different_commit(environment, mismatch):
    env = environment
    submitted = env.service.create_or_reuse(env.app, env.commit)
    (env.source / "app.py").write_text(env.original + "# Different commit\n", encoding="utf-8")
    _git(env.source, "add", "--all")
    _git(env.source, "commit", "--quiet", "-m", "New commit")
    latest = _git(env.source, "rev-parse", "HEAD")
    if mismatch == "resolved_ref":
        env.adapter.resolve_override = latest
    else:
        env.adapter.checkout_override = latest

    assert env.service.execute_static(submitted.check_id, 1)["status"] == "failed"
    result = env.repository.get_route_check(submitted.check_id)
    assert result.static_check_passed is False
    assert "commit" in result.static_report_text.lower()
    assert env.agent.calls == []


@pytest.mark.parametrize("invalid_output", [
    {"route_found": 1, "patch_available": False, "diff_list": [], "report_text": ""},
    {"route_found": True, "patch_available": "no", "diff_list": [], "report_text": ""},
    {"route_found": True, "patch_available": False, "diff_list": [42], "report_text": ""},
    {"route_found": True, "patch_available": False, "diff_list": [], "report_text": None},
    _output(probe_payload=None), _output(probe_payload=_probe("/different-route")),
])
def test_invalid_agent_output_or_probe_fails_closed_and_is_persisted(environment, invalid_output):
    env = environment
    env.agent.output = invalid_output
    submitted = env.service.create_or_reuse(env.app, env.commit)

    assert env.service.execute_static(submitted.check_id, 1)["status"] == "failed"
    result = env.repository.get_route_check(submitted.check_id)
    assert result.static_check_passed is False
    assert result.static_report_text
    assert result.static_run_token is None
    assert result.static_lease_expires_at is None
    _assert_original_and_temporary_worktrees_are_clean(env)


def test_invalid_probe_reports_safe_reason_without_exposing_agent_values(environment):
    env = environment
    secret_destination = "https://private.example.invalid/secret"
    env.agent.output = _output(probe_payload={
        **_probe(),
        "url": secret_destination,
    })
    submitted = env.service.create_or_reuse(env.app, env.commit)

    assert env.service.execute_static(submitted.check_id, 1)["status"] == "failed"
    result = env.repository.get_route_check(submitted.check_id)
    assert result.static_check_passed is False
    assert "Invalid probe payload" in result.static_report_text
    assert "explicit destination" in result.static_report_text
    assert secret_destination not in result.static_report_text


@pytest.mark.parametrize("failure,diagnostic", [
    ("timeout", "configured timeout"), ("invalid_json", "invalid or oversized structured output"),
    ("schema", "does not match"), ("nonzero", "did not complete successfully"),
    ("missing_cli", "not installed"), ("unexpected", "静态检查执行失败"),
])
def test_cli_and_runtime_errors_are_safely_persisted_without_raw_output(environment, failure, diagnostic):
    env = environment
    sensitive = "private-token-should-never-be-persisted"
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1, output=sensitive, stderr=sensitive)
        output = Path(command[command.index("--output-last-message") + 1])
        if failure == "invalid_json":
            response = sensitive
        elif failure == "schema":
            # Valid CLI transport, but an invalid business response. This
            # exercises local role-schema validation rather than the
            # executor's earlier transport-contract validation.
            response = json.dumps({"response_json": json.dumps({"private": sensitive})})
        else:
            response = json.dumps({"private": sensitive})
        output.write_text(response, encoding="utf-8")
        return subprocess.CompletedProcess(command, 2 if failure == "nonzero" else 0, sensitive, sensitive)

    if failure == "unexpected":
        env.service.executor = FakeAgent(error=RuntimeError(sensitive))
    else:
        executable = "/not-installed/codex-static-check-test" if failure == "missing_cli" else sys.executable
        env.service.executor = CodexCliExecutor(executable, runner=runner)
    submitted = env.service.create_or_reuse(env.app, env.commit)

    assert env.service.execute_static(submitted.check_id, 1)["status"] == "failed"
    result = env.repository.get_route_check(submitted.check_id)
    assert result.static_check_passed is False
    assert diagnostic in result.static_report_text
    assert sensitive not in result.model_dump_json()
    assert result.static_run_token is None
    assert result.static_lease_expires_at is None
    if failure not in {"missing_cli", "unexpected"}:
        assert len(calls) == 1
        assert calls[0][1]["stdout"] == subprocess.DEVNULL
        assert calls[0][1]["stderr"] == subprocess.DEVNULL
    _assert_original_and_temporary_worktrees_are_clean(env)


def test_force_while_old_agent_is_running_prevents_the_old_worker_from_overwriting_new_generation(environment):
    env = environment
    started, release = Event(), Event()

    def block(root):
        started.set()
        assert release.wait(timeout=10)

    env.agent.action = block
    original = env.service.create_or_reuse(env.app, env.commit)
    with ThreadPoolExecutor(max_workers=2) as executor:
        old_worker = executor.submit(env.service.execute_static, original.check_id, 1)
        try:
            assert started.wait(timeout=10)
            forced = env.service.create_or_reuse(env.app, env.commit, force=True)
            assert forced.result.generation == 2
        finally:
            release.set()
        assert old_worker.result(timeout=10)["status"] == "superseded"
    result = env.repository.get_route_check(original.check_id)
    assert result.generation == 2
    assert result.static_status == "queued"
    assert result.static_check_passed is None
    assert env.service.execute_static(original.check_id, 2)["status"] == "succeeded"
    assert len(env.agent.calls) == 2


def test_duplicate_worker_deliveries_run_the_agent_only_once(environment):
    env = environment
    started, release = Event(), Event()

    def block(root):
        started.set()
        assert release.wait(timeout=10)

    env.agent.action = block
    submitted = env.service.create_or_reuse(env.app, env.commit)
    with ThreadPoolExecutor(max_workers=8) as executor:
        worker = executor.submit(env.service.execute_static, submitted.check_id, 1)
        try:
            assert started.wait(timeout=10)
            duplicates = [executor.submit(env.service.execute_static, submitted.check_id, 1) for _ in range(7)]
            assert [future.result(timeout=10)["status"] for future in duplicates] == ["running"] * 7
        finally:
            release.set()
        assert worker.result(timeout=10)["status"] == "succeeded"
    assert env.service.execute_static(submitted.check_id, 1)["status"] == "succeeded"
    assert len(env.agent.calls) == 1


def test_recover_schedules_queued_and_expired_running_jobs_but_not_live_leases_or_terminal_jobs(environment):
    env = environment
    expired = env.service.create_or_reuse(env.app, env.commit).result
    queued = env.service.create_or_reuse(env.app.model_copy(update={"require_route": "/queued"}), env.commit).result
    live = env.service.create_or_reuse(env.app.model_copy(update={"require_route": "/live"}), env.commit).result
    terminal = env.service.create_or_reuse(env.app.model_copy(update={"require_route": "/failed"}), env.commit).result
    env.repository.update_route_check(expired.check_id, {
        "static_status": "running", "static_run_token": "crashed-worker", "static_lease_expires_at": utc_now() - timedelta(seconds=1),
    })
    env.repository.update_route_check(live.check_id, {
        "static_status": "running", "static_run_token": "healthy-worker", "static_lease_expires_at": utc_now() + timedelta(minutes=5),
    })
    env.repository.update_route_check(terminal.check_id, {"static_status": "failed", "static_check_passed": False})
    before = len(env.dispatcher.static_check_generations)

    assert env.service.recover() == {"static_checks_scheduled": 2}
    assert set(env.dispatcher.static_check_generations[before:]) == {(expired.check_id, 1), (queued.check_id, 1)}
    assert env.service.execute_static(expired.check_id, 1)["status"] == "succeeded"
    assert env.repository.get_route_check(expired.check_id).static_run_token is None
    assert env.repository.get_route_check(live.check_id).static_run_token == "healthy-worker"


def test_broker_failure_keeps_durable_queued_record_and_recover_can_redeliver_it(environment):
    env = environment

    class FlakyDispatcher(InMemoryTaskDispatcher):
        failing = True

        def dispatch_static_check(self, check_id, generation=1):
            if self.failing:
                raise RuntimeError("broker secret credential must remain private")
            return super().dispatch_static_check(check_id, generation)

    dispatcher = FlakyDispatcher()
    env.service.dispatcher = dispatcher
    with pytest.raises(RouteCheckUnavailableError, match="saved task will be recovered") as error:
        env.service.create_or_reuse(env.app, env.commit)
    assert "credential" not in str(error.value)
    [saved] = env.repository.list_route_checks(env.app.appid, env.commit)
    assert saved.static_status == "queued"
    assert saved.static_check_passed is None
    assert saved.dispatch_error == "Unable to enqueue static check"
    assert env.service.recover() == {"static_checks_scheduled": 0}
    assert env.repository.get_route_check(saved.check_id).static_status == "queued"
    dispatcher.failing = False

    assert env.service.recover() == {"static_checks_scheduled": 1}
    assert dispatcher.static_check_generations == [(saved.check_id, 1)]
    assert env.service.execute_static(saved.check_id, 1)["status"] == "succeeded"


def test_deleted_or_outdated_tasks_are_noops_and_do_not_resurrect_records(environment):
    env = environment
    submitted = env.service.create_or_reuse(env.app, env.commit)
    assert env.service.execute_static(submitted.check_id, 2)["status"] == "superseded"
    assert env.agent.calls == []
    assert env.service.delete_latest(env.app.appid, env.commit) is True
    assert env.service.execute_static(submitted.check_id, 1)["status"] == "deleted"
    assert env.repository.get_route_check(submitted.check_id) is None


def test_deleting_a_running_check_does_not_let_the_old_worker_recreate_it(environment):
    env = environment
    submitted = env.service.create_or_reuse(env.app, env.commit)
    env.agent.action = lambda root: env.service.delete_latest(env.app.appid, env.commit)
    assert env.service.execute_static(submitted.check_id, 1)["status"] == "superseded"
    assert env.repository.get_route_check(submitted.check_id) is None
    _assert_original_and_temporary_worktrees_are_clean(env)


def test_missing_static_result_and_missing_route_are_rejected_by_both_build_gates(environment):
    env = environment
    with pytest.raises(RouteCheckInputError, match=BUILD_GATE_ERROR):
        env.service.require_static_pass(env.app, env.commit)
    result = _complete(env, env.service.create_or_reuse(env.app, env.commit).result)
    no_route = env.app.model_copy(update={"require_route": None})
    with pytest.raises(RouteCheckInputError, match=BUILD_GATE_ERROR):
        env.service.verify_build_gate(_build(env, result, require_route_snapshot=None), no_route)


def test_dynamic_role_change_reuses_static_cache_but_force_snapshots_the_new_dynamic_role(environment):
    env = environment
    original = env.service.create_or_reuse(env.app, env.commit)
    _complete(env, original.result)
    dynamic = env.service.roles.update(DYNAMIC_ROLE, AgentRoleUpdate(prompt="Updated debugger"))

    cached = env.service.create_or_reuse(env.app, env.commit)
    assert cached.cache_hit is True
    assert cached.check_id == original.check_id
    assert cached.result.dynamic_agent_role_version == 1
    forced = env.service.create_or_reuse(env.app, env.commit, force=True)
    assert forced.result.generation == 2
    assert forced.result.dynamic_agent_role_version == dynamic.agent_role_version == 2
    assert forced.result.dynamic_role_snapshot["prompt"] == "Updated debugger"


def test_concurrent_static_submissions_enqueue_only_one_generation(environment):
    env = environment
    barrier = Barrier(8)

    def submit(index):
        barrier.wait(timeout=10)
        return env.service.create_or_reuse(env.app, env.commit)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(submit, range(8)))
    assert len({result.check_id for result in results}) == 1
    assert len(env.dispatcher.static_check_generations) == 1
    [saved] = env.repository.list_route_checks(env.app.appid, env.commit)
    assert saved.static_task_id == env.dispatcher.dispatched[0].task_id


def test_oversized_agent_patch_fails_with_a_bounded_saved_diagnostic(environment):
    env = environment
    env.agent.output = _output(route_found=False, patch_available=True, diff_list=["x" * (1048576 + 1)])
    submitted = env.service.create_or_reuse(env.app, env.commit)

    assert env.service.execute_static(submitted.check_id, 1)["status"] == "failed"
    result = env.repository.get_route_check(submitted.check_id)
    assert result.static_check_passed is False
    assert result.diff_list == []
    assert "one MiB" in result.static_report_text
    assert len(result.static_report_text) < 200
    _assert_original_and_temporary_worktrees_are_clean(env)
