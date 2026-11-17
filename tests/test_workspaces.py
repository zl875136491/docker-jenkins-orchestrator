from copy import deepcopy
from io import BytesIO
from pathlib import Path
import tarfile

import pytest
from fastapi.testclient import TestClient
from jose import jwt

from main import create_app
from orchestrator.config import Settings
from orchestrator.models import BuildCreate, UserAppCreate
from orchestrator.worker_runtime import WorkerRuntime
from orchestrator.workspaces import ComposeConfigError, WorkspaceError, WorkspaceLimitError, WorkspaceStore
from test_worker_runtime import FakeJenkins, FakeDockerServices, FakeGitLab, successful_deployment
from orchestrator.jenkins import JenkinsBuild, JenkinsQueueItem, JenkinsResultArtifact
from tests.route_check_fixtures import TEST_COMMIT_SHA, PassingDynamicChecks, prepare_static_pass


@pytest.fixture
def client(tmp_path):
    settings = Settings(workspace_root=tmp_path / "storage", workspace_host_root=Path("/srv/workspaces"),
                        workspace_download_base_url="http://testserver")
    with TestClient(create_app(settings)) as client:
        token = client.post("/oauth2/token", json={"client_id": settings.worker_name, "client_secret": settings.worker_secret}).json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


def register(client, appid="APP-One", compose=None):
    response = client.post("/api/v1/jenkins/app_create", json={
        "appid": appid, "name": appid,
        "compose": compose or {"services": {"web": {"image": "python:3.12", "volumes": [".:/workspace"]}}},
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_registration_reserves_workspace_and_upload_requires_registered_app(client):
    assert client.post("/workspace/missing", files={"files": ("main.py", b"hello")}).status_code == 404
    register(client)
    store = client.app.state.container.workspace
    assert (store.app_path("APP-One") / "files").is_dir()
    response = client.post("/workspace/APP-One", files=[("files", ("main.py", b"print('hello')")), ("files", ("config/app.json", b"{}"))])
    assert response.status_code == 201, response.text
    assert {item["path"] for item in response.json()["files"]} == {"main.py", "config/app.json"}
    assert client.get("/api/v1/workspace/APP-One").json()["total_bytes"] == 16
    client.headers.pop("Authorization")
    assert client.post("/workspace/APP-One", files={"files": ("main.py", b"bad")}).status_code == 401


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", "a/../../escape", "~/file", "a\\b", ".git/config", "a:ro", "."])
def test_upload_rejects_unsafe_paths_without_writes(client, path):
    register(client)
    response = client.post("/api/v1/workspace/APP-One", files={"files": (path, b"bad")})
    assert response.status_code == 422, response.text
    assert client.get("/api/v1/workspace/APP-One").json()["files"] == []


def test_upload_is_scoped_case_sensitive_and_supports_prefix_and_replacement(client):
    register(client, "Foo")
    register(client, "foo")
    for content in (b"old", b"new"):
        response = client.post("/workspace/Foo", data={"path": "config"}, files={"files": ("value.txt", content)})
        assert response.status_code == 201, response.text
    store = client.app.state.container.workspace
    assert (store.app_path("Foo") / "files/config/value.txt").read_bytes() == b"new"
    assert client.get("/api/v1/workspace/foo").json()["files"] == []


def test_delete_workspace_file_is_scoped_and_preserves_build_snapshots(client):
    register(client, "Foo")
    register(client, "foo")
    filename = "config/配置 #&?.json"
    for appid in ("Foo", "foo"):
        assert client.post(f"/workspace/{appid}", files={"files": (filename, b"{}")}).status_code == 201
    store = client.app.state.container.workspace
    store.snapshot("Foo", "a" * 32)
    response = client.delete("/api/v1/workspace/Foo", params={"path": filename})
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert client.get("/api/v1/workspace/Foo").json() == {"appid": "Foo", "files": [], "total_bytes": 0}
    assert store.ensure_app("Foo").is_dir()
    assert not (store.ensure_app("Foo") / "config").exists()
    assert client.get("/api/v1/workspace/foo").json()["files"][0]["path"] == filename
    assert (store.snapshot_path("Foo", "a" * 32) / "files" / filename).read_bytes() == b"{}"


def test_delete_workspace_file_requires_authentication_registered_app_and_existing_file(client):
    register(client)
    endpoint = "/api/v1/workspace/APP-One"
    assert client.delete(endpoint, params={"path": "missing"}).status_code == 404
    assert client.delete("/api/v1/workspace/missing", params={"path": "file"}).status_code == 404
    assert client.delete(endpoint).status_code == 422
    assert client.delete(endpoint, params={"path": ""}).status_code == 422
    client.headers.pop("Authorization")
    assert client.delete(endpoint, params={"path": "file"}).status_code == 401


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", "a/../../escape", "~/file", "a\\b", ".git/config", "a:ro", "."])
def test_delete_workspace_rejects_unsafe_paths_without_changing_files(client, path):
    register(client)
    client.post("/workspace/APP-One", files={"files": ("main.py", b"keep")})
    response = client.delete("/api/v1/workspace/APP-One", params={"path": path})
    assert response.status_code == 422, response.text
    assert client.get("/api/v1/workspace/APP-One").json()["total_bytes"] == 4


def test_delete_workspace_rejects_directories_symlinks_and_preserves_siblings(client, tmp_path):
    register(client)
    client.post("/workspace/APP-One", files=[("files", ("config/first", b"1")), ("files", ("config/second", b"2"))])
    store = client.app.state.container.workspace
    root = store.ensure_app("APP-One")
    endpoint = "/api/v1/workspace/APP-One"
    assert client.delete(endpoint, params={"path": "config"}).status_code == 422
    assert client.delete(endpoint, params={"path": "config/first"}).status_code == 204
    assert (root / "config/second").read_bytes() == b"2"
    outside = tmp_path / "outside"
    outside.write_bytes(b"safe")
    (root / "link").symlink_to(outside)
    (root / "linked-directory").symlink_to(tmp_path, target_is_directory=True)
    for path in ("link", "linked-directory/outside"):
        assert client.delete(endpoint, params={"path": path}).status_code == 422
    assert outside.read_bytes() == b"safe"
    assert (root / "link").is_symlink()


def test_delete_workspace_returns_storage_failure_and_reclaims_quota(client, monkeypatch, tmp_path):
    register(client)

    def unavailable(appid, path):
        raise PermissionError("storage unavailable")

    monkeypatch.setattr(client.app.state.container.workspace, "delete_file", unavailable)
    response = client.delete("/api/v1/workspace/APP-One", params={"path": "main.py"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Workspace storage is unavailable"
    store = WorkspaceStore(tmp_path / "quota", max_bytes=4, max_files=1)
    store.upload("demo", [("nested/first", BytesIO(b"1234"))])
    store.delete_file("demo", "nested/first")
    store.upload("demo", [("second", BytesIO(b"1234"))])
    assert store.list_files("demo")["files"][0]["path"] == "second"


def test_upload_rejects_symlinks_conflicts_and_limits(tmp_path):
    store = WorkspaceStore(tmp_path / "storage", max_upload_bytes=4, max_bytes=5, max_files=2)
    root = store.ensure_app("demo")
    store.upload("demo", [("a", BytesIO(b"old"))])
    with pytest.raises(WorkspaceLimitError):
        store.upload("demo", [("a", BytesIO(b"12345"))])
    assert (root / "a").read_bytes() == b"old"
    with pytest.raises(WorkspaceLimitError):
        store.upload("demo", [("b", BytesIO(b"123"))])
    with pytest.raises(WorkspaceError, match="overlap"):
        store.upload("demo", [("b", BytesIO(b"1")), ("b/c", BytesIO(b"2"))])
    with pytest.raises(WorkspaceError, match="conflicts"):
        store.upload("demo", [("a/child", BytesIO(b"1"))])
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(WorkspaceError, match="symbolic link"):
        store.upload("demo", [("link/escape", BytesIO(b"1"))])
    assert not (tmp_path / "escape").exists()


def test_build_snapshot_freezes_uploads_and_compose_and_download_is_build_scoped(client):
    register(client)
    assert client.post("/workspace/APP-One", files={"files": ("main.py", b"version1")}).status_code == 201
    container = client.app.state.container
    prepare_static_pass(container, "APP-One")
    build = container.builds.queue_build("APP-One", BuildCreate())
    runtime = WorkerRuntime(container.settings)
    url = runtime._workspace_archive_url(build, build.compose_snapshot)
    token = runtime._workspace_archive_token(build)
    assert "workspace-download" == jwt.get_unverified_claims(token)["scope"]
    headers = {"Authorization": f"Bearer {token}"}
    archive = client.get(url, headers=headers)
    assert archive.status_code == 200
    with tarfile.open(fileobj=BytesIO(archive.content)) as bundle:
        assert bundle.extractfile("main.py").read() == b"version1"
    assert client.get(url.replace("APP-One", "Other"), headers=headers).status_code == 403
    assert client.get(url, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.get(url).status_code == 403
    assert client.post("/workspace/APP-One", files={"files": ("main.py", b"version2")}).status_code == 201
    container.repository.update_app("APP-One", {"compose": {"services": {"different": {"image": "nginx"}}}})
    stored = container.builds.get_build(build.build_id)
    assert stored.compose_snapshot == build.compose_snapshot
    assert (container.workspace.snapshot_path("APP-One", build.build_id) / "files/main.py").read_bytes() == b"version1"
    resolved = runtime._workspace_compose(build, build.compose_snapshot)
    mount = resolved["services"]["web"]["volumes"][0]
    assert mount.startswith("/srv/workspaces/") and mount.endswith(f"/snapshots/{build.build_id}/files:/workspace")
    assert build.compose_snapshot["services"]["web"]["volumes"] == [".:/workspace"]


def test_workspace_resolves_files_long_mounts_and_rejects_missing_traversal_and_cross_app(client):
    register(client)
    client.post("/workspace/APP-One", files={"files": ("config/app.json", b"{}")})
    container = client.app.state.container
    prepare_static_pass(container, "APP-One")
    build = container.builds.queue_build("APP-One", BuildCreate())
    source = {"services": {"web": {"image": "nginx", "volumes": [
        {"type": "bind", "source": "./config/app.json", "target": "/app.json", "read_only": True}, "data:/data"]}}}
    original = deepcopy(source)
    result = container.workspace.resolve_compose("APP-One", build.build_id, source)
    assert result["services"]["web"]["volumes"][0]["source"].endswith("/files/config/app.json")
    assert result["services"]["web"]["volumes"][0]["read_only"] is True
    assert source == original
    for mount in ("../escape:/data", "./missing:/data", "/srv/workspaces/other/files:/data"):
        source["services"]["web"]["volumes"] = [mount]
        with pytest.raises(WorkspaceError):
            container.workspace.resolve_compose("APP-One", build.build_id, source)


def test_uploaded_workspace_reaches_jenkins_and_swarm_without_git_checkout(client):
    source = {"x-orchestrator-build-source": "workspace", "services": {"web": {
        "build": ".", "working_dir": "/workspace", "command": ["python", "main.py"], "volumes": [".:/workspace:ro"],
    }}}
    register(client, "demo", source)
    assert client.post("/workspace/demo", files=[("files", ("main.py", b"print('hi')")), ("files", ("Dockerfile", b"FROM python:3.12"))]).status_code == 201
    container = client.app.state.container
    prepare_static_pass(container, "demo")
    build = container.builds.queue_build("demo", BuildCreate())
    assert build.git_commit_sha == TEST_COMMIT_SHA
    artifact_compose = deepcopy(source)
    artifact_compose["services"]["web"].pop("build")
    artifact_compose["services"]["web"]["image"] = "harbor.example/apps/demo:42"
    artifact = JenkinsResultArtifact(images=("harbor.example/apps/demo:42",), compose=artifact_compose, _payload={})
    jenkins = FakeJenkins([JenkinsQueueItem(queue_url="https://jenkins.example/queue/item/42/", queue_id=42, build_number=42)],
                          build=JenkinsBuild(number=42, building=False, result="SUCCESS"), artifact=artifact)
    gitlab = FakeGitLab("a" * 40)
    docker = FakeDockerServices(successful_deployment())
    container.settings.harbor_url = "https://harbor.example"
    runtime = WorkerRuntime(container.settings, repository=container.repository, build_service=container.builds,
                            catalog=container.catalog, jenkins=jenkins, gitlab=gitlab, docker_services=docker,
                            poll_scheduler=lambda *args: None, route_checks=container.route_checks,
                            dynamic_checks=PassingDynamicChecks(container))
    assert runtime.start_build(build.build_id, "task")["status"] == "building"
    assert not gitlab.calls
    assert jenkins.requests[0].git_ref == TEST_COMMIT_SHA
    assert jenkins.requests[0].repository_url == "https://git.example/demo.git"
    assert jenkins.requests[0].compose == source
    assert client.get(jenkins.requests[0].workspace_archive_url, headers={"Authorization": f"Bearer {jenkins.requests[0].workspace_archive_token}"}).status_code == 200
    assert runtime.poll_build(build.build_id, 0, "poll")["status"] == "succeeded"
    assert not gitlab.calls
    assert runtime.dynamic_checks.calls == [build.build_id]
    assert container.builds.get_build(build.build_id).dynamic_check_passed is True
    assert docker.calls[0][1]["services"]["web"]["volumes"][0].endswith(f"/snapshots/{build.build_id}/files:/workspace:ro")


def test_upload_limits_are_http_413_and_batch_does_not_partially_write(client):
    register(client)
    client.app.state.container.workspace.max_upload_bytes = 3
    response = client.post("/workspace/APP-One", files=[("files", ("first", b"12")), ("files", ("second", b"34"))])
    assert response.status_code == 413
    assert client.get("/api/v1/workspace/APP-One").json()["files"] == []


def test_chunked_upload_is_limited_before_full_multipart_is_spooled(client):
    register(client)
    client.app.state.container.settings.workspace_max_upload_bytes = 1
    body = b'--test-boundary\r\nContent-Disposition: form-data; name="files"; filename="big"\r\n\r\n' + b"x" * (1024 * 1024 + 2) + b"\r\n--test-boundary--\r\n"
    response = client.post("/workspace/APP-One", content=iter([body[:100], body[100:]]), headers={"Content-Type": "multipart/form-data; boundary=test-boundary"})
    assert response.status_code == 413
    assert client.get("/api/v1/workspace/APP-One").json()["files"] == []


def test_workspace_download_rejects_expired_token(client):
    register(client)
    container = client.app.state.container
    prepare_static_pass(container, "APP-One")
    build = container.builds.queue_build("APP-One", BuildCreate())
    token = jwt.encode({"scope": "workspace-download", "appid": "APP-One", "build_id": build.build_id, "exp": 1}, container.settings.jwt_secret)
    response = client.get(f"/api/v1/workspace/APP-One/builds/{build.build_id}/archive", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_git_source_bind_mount_raises_compose_config_error(client):
    """Git sources cannot use relative bind mounts because files are checked out by Jenkins."""
    compose = {
        "x-orchestrator-build-source": "git",
        "services": {
            "web": {
                "image": "nginx",
                "volumes": ["./data:/app/data"]
            }
        }
    }
    register(client, "APP-GitBind", compose)
    container = client.app.state.container
    prepare_static_pass(container, "APP-GitBind")
    build = container.builds.queue_build("APP-GitBind", BuildCreate())
    
    with pytest.raises(ComposeConfigError) as exc_info:
        container.workspace.resolve_compose("APP-GitBind", build.build_id, compose)
    
    error = exc_info.value
    assert error.code == "COMPOSE_CONFIG_ERROR"
    assert error.subcode == "UNSUPPORTED_GIT_BIND_MOUNT"
    assert error.service == "web"
    assert "config_path" in error.error_detail
    assert "remediation" in error.error_detail
    assert "COPY" in str(error)
    assert "Dockerfile" in str(error)


def test_git_source_bind_mount_error_via_api_returns_422_with_structured_detail(client):
    """API endpoints return 422 with structured error_detail for Compose config errors."""
    compose = {
        "x-orchestrator-build-source": "git",
        "services": {
            "api": {
                "build": ".",
                "volumes": ["./config:/app/config:ro"]
            }
        }
    }
    
    response = client.post("/api/v1/jenkins/app_create", json={
        "appid": "APP-API",
        "name": "Test API",
        "repository_url": "https://git.example/api.git",
        "compose": compose,
    })
    assert response.status_code == 201
    
    # Trigger build which will hit validation
    response = client.post("/api/v1/jenkins/APP-API/builds")
    assert response.status_code == 201
    build_id = response.json()["build_id"]
    
    # The build should fail during validation with structured error
    container = client.app.state.container
    runtime = WorkerRuntime(container.settings)
    result = runtime.start_build(build_id, "task")
    
    assert result["status"] == "failed"
    build = container.builds.get_build(build_id)
    assert build.status.value == "failed"
    assert build.failure_site == "validating"
    assert build.error_detail is not None
    assert build.error_detail.get("code") == "COMPOSE_CONFIG_ERROR"
    assert build.error_detail.get("subcode") == "UNSUPPORTED_GIT_BIND_MOUNT"
    assert build.error_detail.get("service") == "api"
    assert "COPY" in build.error_detail.get("remediation", "")


def test_workspace_source_bind_mount_checks_existence(client):
    """Workspace sources validate that bind mount paths exist and are non-empty."""
    compose = {
        "x-orchestrator-build-source": "workspace",
        "services": {
            "web": {
                "image": "nginx",
                "volumes": ["./static:/usr/share/nginx/html"]
            }
        }
    }
    register(client, "APP-WorkspaceBind", compose)
    container = client.app.state.container
    prepare_static_pass(container, "APP-WorkspaceBind")
    build = container.builds.queue_build("APP-WorkspaceBind", BuildCreate())
    
    # Missing path should raise WorkspaceError (not ComposeConfigError)
    with pytest.raises(WorkspaceError, match="does not exist"):
        container.workspace.resolve_compose("APP-WorkspaceBind", build.build_id, compose)
    
    # Upload the required file
    client.post("/workspace/APP-WorkspaceBind", files={"files": ("static/index.html", b"<html></html>")})
    container.workspace.snapshot("APP-WorkspaceBind", build.build_id)
    
    # Now it should succeed
    resolved = container.workspace.resolve_compose("APP-WorkspaceBind", build.build_id, compose)
    assert "/snapshots/" in resolved["services"]["web"]["volumes"][0]


def test_named_volumes_allowed_for_both_git_and_workspace_sources(client):
    """Named volumes work for both Git and workspace sources."""
    for source_type in ("git", "workspace"):
        compose = {
            "x-orchestrator-build-source": source_type,
            "services": {
                "db": {
                    "image": "postgres:16",
                    "volumes": ["pgdata:/var/lib/postgresql/data"]
                }
            },
            "volumes": {
                "pgdata": {}
            }
        }
        appid = f"APP-{source_type.capitalize()}Vol"
        register(client, appid, compose)
        container = client.app.state.container
        prepare_static_pass(container, appid)
        build = container.builds.queue_build(appid, BuildCreate())
        
        if source_type == "workspace":
            # Need at least one file for workspace source
            client.post(f"/workspace/{appid}", files={"files": ("dummy.txt", b"test")})
            container.workspace.snapshot(appid, build.build_id)
        
        # Named volumes should work for both sources
        resolved = container.workspace.resolve_compose(appid, build.build_id, compose)
        assert resolved["services"]["db"]["volumes"] == ["pgdata:/var/lib/postgresql/data"]


def test_error_detail_sanitization_no_secrets_in_compose_errors(client):
    """ComposeConfigError should not leak secrets or sensitive paths."""
    compose = {
        "x-orchestrator-build-source": "git",
        "services": {
            "app": {
                "image": "myapp",
                "volumes": ["./secret-data:/app/data"],
                "environment": {
                    "SECRET_KEY": "super-secret-value"
                }
            }
        }
    }
    register(client, "APP-Secret", compose)
    container = client.app.state.container
    prepare_static_pass(container, "APP-Secret")
    build = container.builds.queue_build("APP-Secret", BuildCreate())
    
    with pytest.raises(ComposeConfigError) as exc_info:
        container.workspace.resolve_compose("APP-Secret", build.build_id, compose)
    
    error_str = str(exc_info.value)
    error_detail = exc_info.value.error_detail
    
    # Should not contain environment secrets
    assert "super-secret-value" not in error_str
    assert "super-secret-value" not in str(error_detail)
    
    # Should not contain absolute host paths
    assert "/srv/workspaces" not in error_str
    assert "/srv/workspaces" not in str(error_detail)
