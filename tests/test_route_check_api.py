from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from main import create_app
from orchestrator.config import Settings
from orchestrator.container import create_container
from orchestrator.gitlab import (
    GitLabComposeDocument, GitLabError, GitLabHttpAdapter, GitLabNotFoundError,
    GitLabProject, GitLabRepositoryRef,
)
from orchestrator.models import BuildStatus, CleanupStatus, utc_now
from orchestrator.route_checks import BUILD_GATE_ERROR, FAILED_CACHE_MESSAGE, STATIC_ROLE, SUCCESS_CACHE_MESSAGE


SHA = "a" * 40
NEXT_SHA = "b" * 40
PRIVATE_CHECK_FIELDS = {"repository_url", "static_role_snapshot", "dynamic_role_snapshot", "static_run_token"}
COMPOSE = {"services": {"api": {"build": {"context": ".", "dockerfile": "Dockerfile"}, "image": "example/app:1"}}}


class FakeGitLab:
    def __init__(self):
        self.commit_sha = SHA
        self.compose = deepcopy(COMPOSE)
        self.validated = []
        self.failure = None

    def validate_repository_ref(self, repository_url, git_ref):
        self.validated.append((repository_url, git_ref))
        if self.failure is not None:
            raise self.failure
        return GitLabRepositoryRef(GitLabProject(1, "team/app"), git_ref, self.commit_sha, repository_url)

    def discover_compose(self, ref):
        return GitLabComposeDocument("compose.yaml", deepcopy(self.compose), ref.commit_sha)


@pytest.fixture
def api(tmp_path):
    settings = Settings(environment="test", workspace_root=tmp_path / "workspaces")
    container = create_container(settings)
    adapter = FakeGitLab()
    container.applications.gitlab = adapter
    application = create_app(settings=settings, container=container)
    with TestClient(application) as client:
        token = client.post("/oauth2/token", json={
            "client_id": settings.worker_name, "client_secret": settings.worker_secret,
        })
        assert token.status_code == 200
        credentials = token.json()
        yield SimpleNamespace(client=client, container=container, adapter=adapter, settings=settings,
                              repository=container.repository, dispatcher=container.dispatcher,
                              headers={"Authorization": "Bearer " + credentials["access_token"]},
                              refresh_token=credentials["refresh_token"], tmp_path=tmp_path)


def create_checked_app(api, *, appid="api-app", require_route="/health", **fields):
    body = {"appid": appid, "name": "API app", "repository_url": "https://git.example/team/app.git",
            "require_route": require_route, "compose": deepcopy(COMPOSE)}
    body.update(fields)
    response = api.client.post("/api/v1/jenkins/app_create", headers=api.headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def latest_check(api, appid="api-app", commit=SHA):
    app = api.repository.get_app(appid)
    version = api.container.route_checks.roles.get(STATIC_ROLE).agent_role_version
    return api.repository.find_route_check(appid, commit, app.require_route, version)


def approve(api, check=None, *, passed=True):
    check = check or latest_check(api)
    return api.repository.update_route_check(check.check_id, {
        "static_status": "succeeded" if passed else "failed", "static_check_passed": passed,
        "static_report_text": None if passed else "没有实现目标路由",
        "probe_payload": {"protocol": "http", "method": "GET", "path": check.require_route,
                          "query_params": {}, "headers": {}, "body": None,
                          "assertions": {"expected_status_list": [200]}} if passed else None,
    })


def results_path(appid="api-app", commit=SHA):
    return f"/api/v1/apps/{appid}/commits/{commit}/check-results"


def deployed_build(api):
    create_checked_app(api)
    checked = approve(api)
    response = api.client.post("/api/v1/jenkins/build_create/api-app", headers=api.headers, json={})
    assert response.status_code == 202, response.text
    build = api.repository.update_build(response.json()["build_id"], {
        "status": BuildStatus.DEPLOYING, "service_ids": ["deployment-api"], "dynamic_status": "pending",
    })
    return checked, build


AUTH_ENDPOINTS = [
    ("GET", "/api/v1/agent-roles", None),
    ("GET", "/api/v1/agent-roles/static_build_agent", None),
    ("PUT", "/api/v1/agent-roles/static_build_agent", {"prompt": "Changed"}),
    ("POST", "/api/v1/apps/missing/static-checks", {}),
    ("GET", "/api/v1/apps/missing/check-results", None),
    ("GET", results_path("missing"), None),
    ("DELETE", results_path("missing"), None),
    ("POST", "/api/v1/apps/missing/dynamic-checks", {"build_id": "missing"}),
]


@pytest.mark.parametrize("method,path,body", AUTH_ENDPOINTS)
@pytest.mark.parametrize("credential", ["missing", "basic", "invalid", "refresh"])
def test_all_check_endpoints_require_valid_access_bearer_tokens(api, method, path, body, credential):
    headers = {
        "missing": {}, "basic": {"Authorization": "Basic dXNlcjpwYXNz"},
        "invalid": {"Authorization": "Bearer invalid-token"},
        "refresh": {"Authorization": "Bearer " + api.refresh_token},
    }[credential]
    response = api.client.request(method, path, headers=headers, **({"json": body} if body is not None else {}))
    assert response.status_code == 401
    assert api.dispatcher.dispatched == []
    assert api.container.route_checks.roles.get(STATIC_ROLE).agent_role_version == 1


def test_role_queries_return_both_configurable_defaults(api):
    response = api.client.get("/api/v1/agent-roles", headers=api.headers)
    assert response.status_code == 200
    roles = {role["role_key"]: role for role in response.json()}
    assert set(roles) == {"static_build_agent", "dynamic_debug_agent"}
    for key, role in roles.items():
        assert role["agent_role_version"] == 1
        assert role["prompt"]
        assert role["output_schema"]["type"] == "object"
        assert role["runtime_config"]["authorization_mode"] == "never"
        assert role["runtime_config"]["sandbox_mode"] == "danger-full-access"
        assert role["runtime_config"]["skip_git_repo_check"] is True
        assert api.client.get(f"/api/v1/agent-roles/{key}", headers=api.headers).json() == role


def test_role_updates_increment_even_for_same_value_and_merge_partial_runtime(api):
    path = "/api/v1/agent-roles/static_build_agent"
    original = api.client.get(path, headers=api.headers).json()
    same = api.client.put(path, headers=api.headers, json={"prompt": original["prompt"]})
    assert same.status_code == 200
    assert same.json()["agent_role_version"] == 2
    configured = api.client.put(path, headers=api.headers, json={"runtime_config": {
        "model": "configured-model", "reasoning_effort": "high", "sandbox_mode": "read-only",
        "authorization_mode": "on-request", "skip_git_repo_check": False,
    }})
    assert configured.status_code == 200
    partial = api.client.put(path, headers=api.headers, json={"runtime_config": {"timeout_seconds": 30}})
    assert partial.status_code == 200
    assert partial.json()["agent_role_version"] == 4
    assert partial.json()["runtime_config"] == {**configured.json()["runtime_config"], "timeout_seconds": 30}
    assert api.client.get("/api/v1/agent-roles/dynamic_debug_agent", headers=api.headers).json()["agent_role_version"] == 1


@pytest.mark.parametrize("body", [
    {}, {"prompt": None}, {"prompt": ""}, {"output_schema": None}, {"runtime_config": None},
    {"output_schema": {"type": "invalid"}}, {"output_schema": {"type": "array"}},
    {"runtime_config": {"timeout_seconds": 0}}, {"runtime_config": {"unknown": True}},
    {"agent_role_version": 99},
])
def test_invalid_role_updates_return_422_without_mutating_version(api, body):
    path = "/api/v1/agent-roles/static_build_agent"
    original = api.client.get(path, headers=api.headers).json()
    response = api.client.put(path, headers=api.headers, json=body)
    assert response.status_code == 422
    assert api.client.get(path, headers=api.headers).json() == original


def test_unknown_role_and_role_cas_conflict_have_explicit_error_codes(api, monkeypatch):
    assert api.client.get("/api/v1/agent-roles/missing", headers=api.headers).status_code == 404
    assert api.client.put("/api/v1/agent-roles/missing", headers=api.headers, json={"prompt": "x"}).status_code == 404
    monkeypatch.setattr(api.repository, "save_agent_role", lambda role, expected_version=None: None)
    response = api.client.put("/api/v1/agent-roles/static_build_agent", headers=api.headers, json={"prompt": "x"})
    assert response.status_code == 409
    assert api.container.route_checks.roles.get(STATIC_ROLE).agent_role_version == 1


@pytest.mark.parametrize("body", [None, {}, {"commit_id": SHA.upper()}])
def test_manual_static_check_accepts_optional_body_and_always_forces_new_generation(api, body):
    create_checked_app(api)
    original = approve(api)
    response = api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers,
                               **({"json": body} if body is not None else {}))
    assert response.status_code == 202, response.text
    submission = response.json()
    assert submission["check_id"] == original.check_id
    assert submission["queued"] is True
    assert submission["cache_hit"] is False
    assert submission["message"] == "静态检查任务已提交"
    assert submission["result"]["generation"] == 2
    assert submission["result"]["static_check_passed"] is None
    assert submission["result"]["commit_id"] == SHA
    assert PRIVATE_CHECK_FIELDS.isdisjoint(submission["result"])
    assert api.dispatcher.static_check_generations[-1] == (original.check_id, 2)


@pytest.mark.parametrize("body", [{"commit_id": "main"}, {"commit_id": "a" * 7}, {"commit_id": "g" * 40}, {"force": False}])
def test_manual_static_check_rejects_mutable_refs_or_unknown_fields(api, body):
    create_checked_app(api)
    response = api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers, json=body)
    assert response.status_code == 422
    assert latest_check(api).generation == 1
    assert len(api.dispatcher.static_check_generations) == 1


def test_static_check_reports_missing_app_route_and_wrong_resolved_commit(api):
    assert api.client.post("/api/v1/apps/missing/static-checks", headers=api.headers).status_code == 404
    create_checked_app(api, require_route=None)
    assert api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers).status_code == 422
    api.repository.update_app("api-app", {"require_route": "/health"})
    api.adapter.commit_sha = NEXT_SHA
    response = api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers, json={"commit_id": SHA})
    assert response.status_code == 422
    assert "different commit" in response.json()["detail"]


@pytest.mark.parametrize("passed,message", [(True, SUCCESS_CACHE_MESSAGE), (False, FAILED_CACHE_MESSAGE)])
def test_metadata_update_uses_success_or_failure_cache_but_manual_post_ignores_it(api, passed, message):
    create_checked_app(api)
    original = approve(api, passed=passed)
    before = len(api.dispatcher.static_check_generations)
    updated = api.client.patch("/api/v1/jenkins/app_info/api-app", headers=api.headers, json={"name": "Metadata update"})
    assert updated.status_code == 200
    assert len(api.dispatcher.static_check_generations) == before
    cached = api.container.route_checks.schedule_for_app(api.repository.get_app("api-app"))
    assert cached.message == message
    assert cached.cache_hit is True
    assert cached.check_id == original.check_id
    forced = api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers, json={})
    assert forced.status_code == 202
    assert forced.json()["result"]["generation"] == 2
    assert len(api.dispatcher.static_check_generations) == before + 1


def test_app_create_metadata_update_and_sync_schedule_the_latest_commit(api):
    app = create_checked_app(api)
    first = latest_check(api)
    assert first.commit_id == SHA
    assert api.dispatcher.static_check_generations == [(first.check_id, 1)]
    api.adapter.commit_sha = NEXT_SHA
    updated = api.client.patch("/api/v1/jenkins/app_info/api-app", headers=api.headers, json={"name": "New metadata"})
    assert updated.status_code == 200
    second = latest_check(api, commit=NEXT_SHA)
    assert second is not None
    assert len(api.dispatcher.static_check_generations) == 2
    api.adapter.commit_sha = "c" * 40
    synchronized = api.client.post("/api/v1/jenkins/app_sync/api-app", headers=api.headers)
    assert synchronized.status_code == 200
    assert synchronized.json()["commit_id"] == "c" * 40
    assert latest_check(api, commit="c" * 40) is not None
    assert len(api.dispatcher.static_check_generations) == 3
    assert api.adapter.validated == [(app["repository_url"], "main")] * 3


def test_results_return_all_historical_routes_and_role_versions_and_delete_only_latest(api):
    create_checked_app(api)
    first = approve(api)
    api.client.put("/api/v1/agent-roles/static_build_agent", headers=api.headers, json={"prompt": "Version 2"})
    second_response = api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers)
    assert second_response.status_code == 202
    second = latest_check(api)
    assert second.static_agent_role_version == 2
    changed = api.client.patch("/api/v1/jenkins/app_info/api-app", headers=api.headers, json={"require_route": "/new"})
    assert changed.status_code == 200
    third = latest_check(api)
    assert third.require_route == "/new"
    records = api.client.get(results_path(), headers=api.headers)
    assert records.status_code == 200
    assert {item["check_id"] for item in records.json()} == {first.check_id, second.check_id, third.check_id}
    assert all(PRIVATE_CHECK_FIELDS.isdisjoint(item) for item in records.json())

    deleted = api.client.delete(results_path(), headers=api.headers)
    assert deleted.status_code == 200
    assert deleted.json() is True
    assert api.client.delete(results_path(), headers=api.headers).json() is False
    assert api.repository.get_route_check(third.check_id) is None
    assert api.repository.get_route_check(first.check_id) is not None
    assert api.repository.get_route_check(second.check_id) is not None
    assert {item["check_id"] for item in api.client.get(results_path(), headers=api.headers).json()} == {first.check_id, second.check_id}


def test_app_results_return_historical_checks_across_all_commits(api):
    create_checked_app(api)
    first = approve(api)
    api.adapter.commit_sha = NEXT_SHA
    second_response = api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers)
    assert second_response.status_code == 202
    second = api.repository.get_route_check(second_response.json()["check_id"])
    assert second is not None and second.commit_id == NEXT_SHA

    response = api.client.get("/api/v1/apps/api-app/check-results", headers=api.headers)
    assert response.status_code == 200
    assert [item["check_id"] for item in response.json()] == [second.check_id, first.check_id]
    assert all(PRIVATE_CHECK_FIELDS.isdisjoint(item) for item in response.json())


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_results_endpoints_distinguish_invalid_commit_missing_app_and_empty_history(api, method):
    assert api.client.request(method, results_path("missing"), headers=api.headers).status_code == 404
    create_checked_app(api)
    assert api.client.request(method, results_path(commit="short"), headers=api.headers).status_code == 422
    response = api.client.request(method, results_path(commit=NEXT_SHA), headers=api.headers)
    assert response.status_code == 200
    assert response.json() == ([] if method == "GET" else False)


def test_results_include_persisted_dynamic_diagnostics_and_hide_internal_snapshots_and_token(api):
    create_checked_app(api)
    check = approve(api)
    api.repository.update_route_check(check.check_id, {
        "dynamic_agent_role_version": 3, "dynamic_status": "failed", "dynamic_check_passed": False,
        "probe_result": {"attempts": [{"status_code": 503, "passed": False}]},
        "dynamic_report_text": "目标路由返回503，日志显示依赖未就绪", "static_run_token": "private-worker-token",
        "dynamic_role_snapshot": {"prompt": "private-snapshot"},
    })
    response = api.client.get(results_path(), headers=api.headers)
    assert response.status_code == 200
    [result] = response.json()
    assert result["dynamic_agent_role_version"] == 3
    assert result["dynamic_status"] == "failed"
    assert result["dynamic_check_passed"] is False
    assert result["probe_result"] == {"attempts": [{"status_code": 503, "passed": False}]}
    assert result["dynamic_report_text"] == "目标路由返回503，日志显示依赖未就绪"
    assert PRIVATE_CHECK_FIELDS.isdisjoint(result)
    assert "private-worker-token" not in response.text
    assert "private-snapshot" not in response.text


@pytest.mark.parametrize("failure,status", [(GitLabNotFoundError("Repository ref not found"), 422), (GitLabError("GitLab request failed"), 502)])
def test_gitlab_errors_on_static_submission_keep_existing_safe_http_mapping(api, failure, status):
    create_checked_app(api)
    api.adapter.failure = failure
    response = api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers)
    assert response.status_code == status
    assert response.json()["detail"] == str(failure)


def test_static_broker_failure_returns_503_and_keeps_recoverable_new_generation(api, monkeypatch):
    create_checked_app(api)
    dispatch = api.dispatcher.dispatch_static_check

    def broken_dispatch(*args, **kwargs):
        raise RuntimeError("private-broker-password")

    monkeypatch.setattr(api.dispatcher, "dispatch_static_check", broken_dispatch)
    response = api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers)
    assert response.status_code == 503
    assert "private-broker-password" not in response.text
    saved = latest_check(api)
    assert saved.generation == 2
    assert saved.static_status == "queued"
    assert saved.dispatch_error == "Unable to enqueue static check"
    monkeypatch.setattr(api.dispatcher, "dispatch_static_check", dispatch)
    assert api.container.route_checks.recover() == {"static_checks_scheduled": 1}
    assert api.dispatcher.static_check_generations[-1] == (saved.check_id, 2)


def test_dynamic_submission_uses_real_service_dispatch_and_returns_queued_build(api):
    check, build = deployed_build(api)
    response = api.client.post("/api/v1/apps/api-app/dynamic-checks", headers=api.headers, json={"build_id": build.build_id})
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["build_id"] == build.build_id
    assert payload["appid"] == "api-app"
    assert payload["status"] == "deploying"
    assert payload["dynamic_status"] == "queued"
    assert payload["dynamic_check_passed"] is None
    assert payload["dynamic_run_id"]
    assert {"compose_snapshot", "workspace_snapshot", "route_probe_payload", "route_dynamic_role_snapshot",
            "dynamic_retry_payload"}.isdisjoint(payload)
    assert api.dispatcher.dynamic_check_runs == [(build.build_id, payload["dynamic_run_id"])]
    saved_check = api.client.get(results_path(), headers=api.headers).json()[0]
    assert saved_check["check_id"] == check.check_id
    assert saved_check["dynamic_status"] == "queued"
    assert saved_check["dynamic_check_passed"] is None


def test_build_create_detail_and_history_hide_internal_route_and_compose_snapshots(api):
    create_checked_app(api)
    checked = approve(api)
    created = api.client.post("/api/v1/jenkins/build_create/api-app", headers=api.headers, json={})
    assert created.status_code == 202, created.text
    build_id = created.json()["build_id"]
    private_fields = {"compose_snapshot", "workspace_snapshot", "route_probe_payload",
                      "route_dynamic_role_snapshot", "dynamic_retry_payload"}
    assert private_fields.isdisjoint(created.json())

    api.repository.update_build(build_id, {
        "compose_snapshot": {"services": {"api": {"environment": {"PASSWORD": "private-compose-secret"}}}},
        "workspace_snapshot": "/private/workspace/snapshot.tar",
        "route_probe_payload": {"headers": {"Authorization": "Bearer private-probe-token"}},
        "route_dynamic_role_snapshot": {"prompt": "private-role-snapshot"},
        "dynamic_retry_payload": {"headers": {"Authorization": "Bearer private-retry-token"}},
        "dynamic_status": "failed", "dynamic_check_passed": False,
        "probe_result": {"final": {"status_code": 503}},
        "dynamic_report_text": "依赖服务未就绪",
    })

    detail = api.client.get(f"/api/v1/jenkins/build_info/{build_id}", headers=api.headers)
    history = api.client.get("/api/v1/jenkins/build_list?app_id=api-app", headers=api.headers)
    assert detail.status_code == history.status_code == 200
    [listed] = history.json()["items"]
    for payload in (detail.json(), listed):
        assert private_fields.isdisjoint(payload)
        assert "private" not in str(payload)
        assert payload["dynamic_check_passed"] is False
        assert payload["probe_result"] == {"final": {"status_code": 503}}
        assert payload["dynamic_report_text"] == "依赖服务未就绪"


def test_dynamic_submission_is_idempotent_for_the_same_queued_run(api):
    _, build = deployed_build(api)
    first = api.client.post("/api/v1/apps/api-app/dynamic-checks", headers=api.headers, json={"build_id": build.build_id})
    second = api.client.post("/api/v1/apps/api-app/dynamic-checks", headers=api.headers, json={"build_id": build.build_id})
    assert first.status_code == second.status_code == 202
    assert first.json()["dynamic_run_id"] == second.json()["dynamic_run_id"]
    assert len(api.dispatcher.dynamic_check_runs) == 1


@pytest.mark.parametrize("body", [{}, {"build_id": ""}, {"build_id": "x" * 65}, {"build_id": "a", "force": True}])
def test_dynamic_submission_invalid_body_returns_422(api, body):
    response = api.client.post("/api/v1/apps/api-app/dynamic-checks", headers=api.headers, json=body)
    assert response.status_code == 422


def test_dynamic_submission_missing_or_cross_app_build_returns_404(api):
    _, build = deployed_build(api)
    assert api.client.post("/api/v1/apps/api-app/dynamic-checks", headers=api.headers, json={"build_id": "missing"}).status_code == 404
    create_checked_app(api, appid="another-app")
    assert api.client.post("/api/v1/apps/another-app/dynamic-checks", headers=api.headers, json={"build_id": build.build_id}).status_code == 404
    assert api.client.post("/api/v1/apps/missing/dynamic-checks", headers=api.headers, json={"build_id": build.build_id}).status_code == 404


@pytest.mark.parametrize("unavailable", ["not_deployed", "no_services", "busy", "cleanup"])
def test_dynamic_submission_rejects_unavailable_or_busy_build_resources(api, unavailable):
    _, build = deployed_build(api)
    if unavailable == "not_deployed":
        api.repository.update_build(build.build_id, {"status": BuildStatus.BUILDING})
    elif unavailable == "no_services":
        api.repository.update_build(build.build_id, {"service_ids": []})
    elif unavailable == "cleanup":
        api.repository.update_build(build.build_id, {"teardown_required": True})
    else:
        assert api.repository.claim_build_lease(build.build_id, "another-operation", utc_now() + timedelta(minutes=5), [CleanupStatus.NOT_STARTED])
    response = api.client.post("/api/v1/apps/api-app/dynamic-checks", headers=api.headers, json={"build_id": build.build_id})
    assert response.status_code == 409
    assert api.dispatcher.dynamic_check_runs == []


def test_dynamic_broker_failure_returns_503_and_preserves_durable_run(api, monkeypatch):
    _, build = deployed_build(api)

    def broken_dispatch(*args, **kwargs):
        raise RuntimeError("private-dynamic-broker-password")

    monkeypatch.setattr(api.dispatcher, "dispatch_dynamic_check", broken_dispatch)
    response = api.client.post("/api/v1/apps/api-app/dynamic-checks", headers=api.headers, json={"build_id": build.build_id})
    assert response.status_code == 503
    assert "private-dynamic-broker-password" not in response.text
    saved = api.repository.get_build(build.build_id)
    assert saved.dynamic_status == "queued"
    assert saved.dynamic_run_id
    assert saved.dynamic_dispatch_error == "Unable to enqueue dynamic check"


def _git(root: Path, *args: str):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=API integration",
                           "-c", "user.email=api@example.invalid", "-C", str(root), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class LocalApiGit(GitLabHttpAdapter):
    def __init__(self, source):
        super().__init__("https://git.example", "local-integration-token")
        self.source = source
        self.validated = []

    def validate_repository_ref(self, repository_url, git_ref):
        self.validated.append(git_ref)
        commit = _git(self.source, "rev-parse", "--verify", f"{git_ref}^{{commit}}")
        return GitLabRepositoryRef(GitLabProject(1, "team/app"), git_ref, commit, repository_url)

    def _clone_url(self, ref):
        return self.source.as_uri()


def test_api_real_git_static_execution_cache_result_query_and_build_gate_integration(api):
    source = api.tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--quiet", "--initial-branch=main")
    original = "def health():\n    return {'status': 'ok'}\n"
    (source / "app.py").write_text(original, encoding="utf-8")
    (source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    _git(source, "add", "--all")
    _git(source, "commit", "--quiet", "-m", "Route implementation")
    commit = _git(source, "rev-parse", "HEAD")
    adapter = LocalApiGit(source)
    api.container.applications.gitlab = adapter
    calls = []

    class StaticAgent:
        def run_json(self, role, context, *, cwd):
            root = Path(cwd)
            calls.append((role, deepcopy(context), root))
            assert root != source
            assert _git(root, "rev-parse", "HEAD") == commit
            assert (root / "app.py").read_text(encoding="utf-8") == original
            return {"route_found": True, "patch_available": False, "diff_list": [], "report_text": "",
                    "probe_payload": {"method": "GET", "query_params": {}, "body": None,
                                      "assertions": {"expected_status_list": [200]}}}

    api.container.route_checks.executor = StaticAgent()
    create_checked_app(api)
    blocked = api.client.post("/api/v1/jenkins/build_create/api-app", headers=api.headers, json={})
    assert blocked.status_code == 422
    assert blocked.json()["detail"] == BUILD_GATE_ERROR
    submitted = api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers, json={"commit_id": commit})
    assert submitted.status_code == 202
    job = submitted.json()["result"]
    assert job["commit_id"] == commit
    assert job["generation"] == 2
    assert api.dispatcher.static_check_generations[-1] == (job["check_id"], 2)
    assert api.container.route_checks.execute_static(job["check_id"], 1)["status"] == "superseded"

    assert api.container.route_checks.execute_static(job["check_id"], 2)["status"] == "succeeded"
    [result] = api.client.get(results_path(commit=commit), headers=api.headers).json()
    assert result["static_check_passed"] is True
    assert result["static_status"] == "succeeded"
    assert result["probe_payload"].get("path", result["require_route"]) == "/health"
    assert PRIVATE_CHECK_FIELDS.isdisjoint(result)
    assert len(calls) == 1
    assert not calls[0][2].exists()
    assert _git(source, "status", "--porcelain") == ""
    assert _git(source, "rev-parse", "HEAD") == commit
    assert (source / "app.py").read_text(encoding="utf-8") == original

    dispatched_before = len(api.dispatcher.static_check_generations)
    metadata = api.client.patch("/api/v1/jenkins/app_info/api-app", headers=api.headers, json={"name": "Cached app"})
    assert metadata.status_code == 200
    assert len(api.dispatcher.static_check_generations) == dispatched_before
    cached = api.container.route_checks.schedule_for_app(api.repository.get_app("api-app"))
    assert cached.cache_hit is True
    assert cached.message == SUCCESS_CACHE_MESSAGE
    build_response = api.client.post("/api/v1/jenkins/build_create/api-app", headers=api.headers, json={})
    assert build_response.status_code == 202, build_response.text
    build = build_response.json()
    assert build["git_commit_sha"] == commit
    assert build["route_check_id"] == job["check_id"]
    assert build["route_check_generation"] == 2
    assert build["static_check_passed"] is True
    assert api.dispatcher.build_ids == [build["build_id"]]


@pytest.mark.parametrize("dimension", ["app", "commit", "route", "role"])
def test_build_api_enforces_every_static_cache_dimension_before_enqueuing(api, dimension):
    create_checked_app(api)
    approve(api)
    appid = "api-app"
    if dimension == "app":
        appid = "another-app"
        create_checked_app(api, appid=appid)
    elif dimension == "commit":
        api.adapter.commit_sha = NEXT_SHA
    elif dimension == "route":
        response = api.client.patch("/api/v1/jenkins/app_info/api-app", headers=api.headers, json={"require_route": "/other"})
        assert response.status_code == 200
    else:
        response = api.client.put("/api/v1/agent-roles/static_build_agent", headers=api.headers, json={"prompt": "New version"})
        assert response.status_code == 200

    response = api.client.post(f"/api/v1/jenkins/build_create/{appid}", headers=api.headers, json={})
    assert response.status_code == 422
    assert response.json()["detail"] == BUILD_GATE_ERROR
    assert api.repository.list_builds()[1] == 0
    assert api.dispatcher.build_ids == []


def test_static_force_generation_cas_conflict_returns_409_without_enqueuing(api, monkeypatch):
    create_checked_app(api)
    original_update = api.repository.update_route_check

    def lose_force_race(check_id, fields, expected=None):
        if "generation" in fields:
            return None
        return original_update(check_id, fields, expected)

    monkeypatch.setattr(api.repository, "update_route_check", lose_force_race)
    response = api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers)
    assert response.status_code == 409
    assert "changed concurrently" in response.json()["detail"]
    assert len(api.dispatcher.static_check_generations) == 1
    assert latest_check(api).generation == 1


def test_results_accept_full_64_hex_sha_and_normalize_path_case(api):
    api.adapter.commit_sha = "c" * 64
    create_checked_app(api)
    response = api.client.get(results_path(commit="C" * 64), headers=api.headers)
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["commit_id"] == "c" * 64
    deleted = api.client.delete(results_path(commit="C" * 64), headers=api.headers)
    assert deleted.status_code == 200
    assert deleted.json() is True
    assert api.client.get(results_path(commit="c" * 64), headers=api.headers).json() == []


def test_dynamic_api_rejects_a_build_whose_static_generation_was_superseded(api):
    _, build = deployed_build(api)
    forced = api.client.post("/api/v1/apps/api-app/static-checks", headers=api.headers)
    assert forced.status_code == 202
    assert forced.json()["result"]["generation"] == 2

    response = api.client.post("/api/v1/apps/api-app/dynamic-checks", headers=api.headers, json={"build_id": build.build_id})
    assert response.status_code == 422
    assert response.json()["detail"] == BUILD_GATE_ERROR
    assert api.dispatcher.dynamic_check_runs == []


def test_dynamic_api_uses_latest_debug_role_and_keeps_its_snapshot_private(api):
    _, build = deployed_build(api)
    role = api.client.put("/api/v1/agent-roles/dynamic_debug_agent", headers=api.headers,
                          json={"prompt": "Updated debugger configuration"})
    assert role.status_code == 200
    response = api.client.post("/api/v1/apps/api-app/dynamic-checks", headers=api.headers, json={"build_id": build.build_id})
    assert response.status_code == 202
    assert response.json()["dynamic_agent_role_version"] == 2
    assert "route_dynamic_role_snapshot" not in response.json()
    stored = api.repository.get_build(build.build_id)
    assert stored.route_dynamic_role_snapshot["prompt"] == "Updated debugger configuration"
    [result] = api.client.get(results_path(), headers=api.headers).json()
    assert result["dynamic_agent_role_version"] == 2
    assert "dynamic_role_snapshot" not in result


def test_app_creation_broker_failure_returns_503_with_durable_check_for_recovery(api, monkeypatch):
    dispatch = api.dispatcher.dispatch_static_check

    def fail_dispatch(*args, **kwargs):
        raise RuntimeError("private-create-broker-password")

    monkeypatch.setattr(api.dispatcher, "dispatch_static_check", fail_dispatch)
    response = api.client.post("/api/v1/jenkins/app_create", headers=api.headers, json={
        "appid": "api-app", "name": "App with durable failed scheduling", "require_route": "/health",
        "repository_url": "https://git.example/team/app.git", "compose": deepcopy(COMPOSE),
    })
    assert response.status_code == 503
    assert "private-create-broker-password" not in response.text
    assert api.repository.get_app("api-app") is not None
    saved = latest_check(api)
    assert saved.static_status == "queued"
    assert saved.dispatch_error == "Unable to enqueue static check"
    monkeypatch.setattr(api.dispatcher, "dispatch_static_check", dispatch)
    assert api.container.route_checks.recover() == {"static_checks_scheduled": 1}
    assert api.dispatcher.static_check_generations == [(saved.check_id, 1)]
