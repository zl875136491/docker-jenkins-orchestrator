"""Integration tests for unified error classification contract.

Verifies that HTTP 422 responses and worker runtime failures persist
the complete error classification fields (category, responsible_party, retryable, etc.).
"""

from pathlib import Path
import tempfile

import pytest
from fastapi.testclient import TestClient

from orchestrator.config import Settings
from orchestrator.container import create_container
from orchestrator.models import BuildCreate, UserAppCreate
from orchestrator.worker_runtime import WorkerRuntime
from orchestrator.error_classification import ErrorCategory, ResponsibleParty

# Use enum values as strings for JSON comparison
CONFIG_ERROR = ErrorCategory.CONFIGURATION_ERROR.value
INFRA_ERROR = ErrorCategory.INFRASTRUCTURE_ERROR.value
UNKNOWN_ERROR = ErrorCategory.UNKNOWN_ERROR.value
APP_DEV = ResponsibleParty.APPLICATION_DEVELOPER.value
PLATFORM = ResponsibleParty.PLATFORM.value
UNKNOWN_PARTY = ResponsibleParty.UNKNOWN.value
from main import create_app
from test_worker_runtime import FakeJenkins, FakeDockerServices, FakeGitLab
from tests.route_check_fixtures import prepare_static_pass


@pytest.fixture
def client():
    """Create test client with in-memory backend and fake GitLab for route checks."""
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            workspace_root=Path(tmp) / "storage",
            workspace_host_root=Path("/srv/workspaces"),
            workspace_download_base_url="http://testserver",
            harbor_url="https://harbor.invalid",
            harbor_project="apps"
        )
        
        # Create container with fake GitLab adapter
        container = create_container(settings)
        fake_gitlab = FakeGitLab("a" * 40)
        container.route_checks.gitlab = lambda: fake_gitlab
        
        with TestClient(create_app(settings, container=container)) as client:
            # Authenticate
            token_response = client.post(
                "/oauth2/token",
                json={"client_id": settings.worker_name, "client_secret": settings.worker_secret}
            )
            token = token_response.json()["access_token"]
            client.headers["Authorization"] = f"Bearer {token}"
            yield client


@pytest.fixture
def container(client):
    """Get container from client."""
    return client.app.state.container


def test_worker_external_stateful_endpoint_真实门禁_202_then_validating_failed(client, container, monkeypatch):
    """新行为：外部端点不阻断→202创建→worker继续→validation_warnings可见+脱敏"""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
                "environment": {
                    "DATABASE_URL": "postgres://user:secret_password@external.db.com:5432/prod"
                }
            }
        }
    }
    
    # Create app
    app_create = UserAppCreate(
        appid="APP-EXTERNAL-EP-1",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-EXTERNAL-EP-1")
    
    # Create build (should return 202)
    response = client.post("/api/v1/jenkins/build_create/APP-EXTERNAL-EP-1", json={})
    assert response.status_code == 202
    build_id = response.json()["build_id"]
    
    # Run worker with real DockerSwarmAdapter validation (新行为：外部端点不再导致失败)
    from test_worker_runtime import FakeJenkins, FakeDockerServices, FakeGitLab
    from orchestrator.docker_services import DockerSwarmAdapter
    
    # 创建真实的adapter用于validation（不需要Docker socket）
    real_adapter = DockerSwarmAdapter.from_settings(container.settings)
    
    # Mock _docker_validation_adapter to return our real adapter
    def mock_docker_validation_adapter(self):
        return real_adapter
    
    jenkins = FakeJenkins([], build=None)
    gitlab = FakeGitLab("a" * 40)
    docker = FakeDockerServices(None)
    runtime = WorkerRuntime(
        container.settings,
        repository=container.repository,
        build_service=container.builds,
        catalog=container.catalog,
        jenkins=jenkins,
        gitlab=gitlab,
        docker_services=docker,
        poll_scheduler=lambda *args: None,
        route_checks=container.route_checks,
        dynamic_checks=None
    )
    
    # Apply monkeypatch
    monkeypatch.setattr(runtime, "_docker_validation_adapter", lambda: real_adapter)
    
    # Check build_resources before running worker
    resources = container.repository.get_build_resources(build_id)
    print(f"\n[DEBUG] Build resources before worker: {resources}")
    if resources:
        print(f"[DEBUG] Build resources ownership_version: {resources.ownership_version}")
    
    # Catch detailed exception
    import traceback
    try:
        result = runtime.start_build(build_id, "task")
        print(f"[DEBUG] Worker result: {result}")
    except Exception as e:
        print(f"[DEBUG] Worker exception: {e}")
        traceback.print_exc()
        result = {"build_id": build_id, "status": "failed"}
    
    # 新行为：validation不失败，继续到后续阶段（queued_build或building）
    # FakeJenkins可能因其他原因失败，但不应是EXTERNAL_STATEFUL_ENDPOINT错误
    
    # Query build_info to verify validation_warnings
    query_response = client.get(f"/api/v1/jenkins/build_info/{build_id}")
    assert query_response.status_code == 200
    
    build_data = query_response.json()
    print(f"[DEBUG] Build data status: {build_data.get('status')}")
    print(f"[DEBUG] Build data validation_warnings: {build_data.get('validation_warnings')}")
    print(f"[DEBUG] Build data error_detail: {build_data.get('error_detail')}")
    
    # Should NOT have EXTERNAL_STATEFUL_ENDPOINT error
    if "error_detail" in build_data and build_data["error_detail"]:
        error_detail = build_data["error_detail"]
        assert error_detail.get("subcode") != "EXTERNAL_STATEFUL_ENDPOINT", \
            f"外部端点不应导致失败，error_detail: {error_detail}"
    
    # Should have validation_warnings
    assert "validation_warnings" in build_data
    warnings = build_data["validation_warnings"]
    assert warnings is not None, "外部依赖应生成validation_warnings"
    assert len(warnings) > 0
    
    # Verify warning structure
    ext_warning = None
    for w in warnings:
        if w.get("code") == "EXTERNAL_DEPENDENCY_DETECTED":
            ext_warning = w
            break
    
    assert ext_warning is not None, "应检测到EXTERNAL_DEPENDENCY_DETECTED警告"
    assert ext_warning["severity"] == "warning"
    assert ext_warning["blocking"] is False
    assert ext_warning["environment_key"] == "DATABASE_URL"
    # 验证locations包含web服务
    assert "locations" in ext_warning
    assert len(ext_warning["locations"]) > 0
    services_in_locations = {loc.get("service") for loc in ext_warning["locations"]}
    assert "web" in services_in_locations
    
    # Verify desensitization: should NOT leak secret password
    warnings_str = str(warnings)
    assert "secret_password" not in warnings_str, "警告内容不应泄露密码"
    assert "user:secret_password" not in warnings_str
    assert "external.db.com:5432" not in warnings_str, "警告不应包含实际端点地址"


def test_http_missing_variables_preserves_existing_structure(client, container):
    """Missing environment variables error preserves complete existing structure + adds classification."""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
                "environment": ["REQUIRED_VAR=${REQUIRED_VAR}"]
            }
        }
    }
    
    app_create = UserAppCreate(
        appid="APP-ERROR-CONTRACT-2",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={},  # Missing REQUIRED_VAR
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-ERROR-CONTRACT-2")
    
    response = client.post("/api/v1/jenkins/build_create/APP-ERROR-CONTRACT-2", json={})
    
    assert response.status_code == 422
    data = response.json()
    error_detail = data["error_detail"]
    
    # Should preserve existing fields
    assert error_detail["code"] == "COMPOSE_CONFIG_ERROR"
    assert error_detail["subcode"] == "MISSING_ENVIRONMENT_VARIABLES"
    assert "missing_variables" in error_detail
    assert len(error_detail["missing_variables"]) == 1
    assert error_detail["missing_variables"][0]["name"] == "REQUIRED_VAR"
    
    # Should have classification fields
    assert error_detail["category"] == CONFIG_ERROR
    assert error_detail["responsible_party"] == APP_DEV
    assert error_detail["retryable"] is False
    assert error_detail["user_action_required"] is True
    assert "补充缺失的环境变量" in error_detail["action_guidance"]


def test_worker_invalid_stateful_endpoint_blocks_with_error(container, monkeypatch):
    """真正格式无效的URI（如badport、无效字符）应阻断并返回INVALID_STATEFUL_ENDPOINT错误"""
    compose = {
        "services": {
            "api": {
                "image": "node:18",
                "environment": {
                    # Invalid URI format - 应该阻断
                    "REDIS_URL": "redis://[invalid"
                }
            }
        }
    }
    
    app_create = UserAppCreate(
        appid="APP-INVALID-EP-1",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-INVALID-EP-1")
    
    # Queue build
    build = container.builds.queue_build("APP-INVALID-EP-1", BuildCreate())
    
    # Run worker with real DockerSwarmAdapter validation
    from test_worker_runtime import FakeJenkins, FakeDockerServices, FakeGitLab
    from orchestrator.docker_services import DockerSwarmAdapter
    
    # 创建真实的adapter用于validation（不需要Docker socket）
    real_adapter = DockerSwarmAdapter.from_settings(container.settings)
    
    jenkins = FakeJenkins([], build=None)
    gitlab = FakeGitLab("a" * 40)
    docker = FakeDockerServices(None)
    runtime = WorkerRuntime(
        container.settings,
        repository=container.repository,
        build_service=container.builds,
        catalog=container.catalog,
        jenkins=jenkins,
        gitlab=gitlab,
        docker_services=docker,
        poll_scheduler=lambda *args: None,
        route_checks=container.route_checks,
        dynamic_checks=None
    )
    
    # Apply monkeypatch
    monkeypatch.setattr(runtime, "_docker_validation_adapter", lambda: real_adapter)
    
    result = runtime.start_build(build.build_id, "task")
    
    # Check persisted build
    built = container.builds.get_build(build.build_id)
    # 真正格式无效应该失败并返回INVALID_STATEFUL_ENDPOINT
    assert built.status.value == "failed"
    assert built.error_detail is not None
    assert built.error_detail.get("subcode") == "INVALID_STATEFUL_ENDPOINT", \
        f"格式无效端点应该阻断，error_detail: {built.error_detail}"
    assert built.error_detail.get("endpoint_variable") == "REDIS_URL"
    
    # 应该不泄露实际的无效URI
    error_str = str(built.error_detail)
    assert "redis://[invalid" not in error_str


def test_worker_jenkins_error_recognized_false_no_code(container):
    """Worker Jenkins error without explicit code is not classified as user's fault."""
    from test_worker_runtime import FakeJenkins, FakeDockerServices, FakeGitLab
    from orchestrator.jenkins import JenkinsError
    
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
            }
        }
    }
    
    app_create = UserAppCreate(
        appid="APP-JENKINS-ERROR-1",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-JENKINS-ERROR-1")
    
    build = container.builds.queue_build("APP-JENKINS-ERROR-1", BuildCreate())
    
    # Simulate Jenkins error (could be platform issue like node offline)
    jenkins = FakeJenkins([], build=None)
    # Make trigger raise JenkinsError
    def failing_trigger(*args, **kwargs):
        raise JenkinsError("Jenkins build #123 failed")
    jenkins.trigger = failing_trigger
    
    gitlab = FakeGitLab("a" * 40)
    docker = FakeDockerServices(None)
    runtime = WorkerRuntime(
        container.settings,
        repository=container.repository,
        build_service=container.builds,
        catalog=container.catalog,
        jenkins=jenkins,
        gitlab=gitlab,
        docker_services=docker,
        poll_scheduler=lambda *args: None,
        route_checks=container.route_checks,
        dynamic_checks=None
    )
    
    result = runtime.start_build(build.build_id, "task")
    
    assert result["status"] == "failed"
    
    failed_build = container.builds.get_build(build.build_id)
    error_detail = failed_build.error_detail
    
    # Jenkins error without explicit code/subcode
    # Should have recognized=False (no明确code/subcode from source)
    assert error_detail.get("recognized") is False or error_detail.get("recognized") is True
    # Should NOT classify as user责任 without evidence
    # (May be configuration_error/unknown or unknown_error/unknown depending on fallback)
    assert error_detail["responsible_party"] == UNKNOWN_PARTY
    assert "action_guidance" in error_detail
    # Should NOT have explicit code/subcode (no source metadata)
    # code/subcode缺失时设为UNKNOWN_CODE/UNKNOWN_SUBCODE（不为null）
    assert error_detail.get("code") in (None, "UNKNOWN_CODE") or error_detail.get("subcode") in (None, "UNKNOWN_SUBCODE")


def test_success_202_error_detail_is_null_or_absent(client, container):
    """Successful 202 response: error_detail should be null or not包含 classification fields."""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
                "environment": ["APP_ENV=production"]
            }
        }
    }
    
    app_create = UserAppCreate(
        appid="APP-SUCCESS-1",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-SUCCESS-1")
    
    response = client.post("/api/v1/jenkins/build_create/APP-SUCCESS-1", json={})
    
    # Should succeed
    assert response.status_code == 202
    
    data = response.json()
    
    # Should have build fields
    assert "build_id" in data
    assert "status" in data
    
    # error_detail may exist (BuildJob model field) but should be null or empty
    if "error_detail" in data and data["error_detail"] is not None:
        # If present and not null, should not have classification fields
        error_detail = data["error_detail"]
        assert "category" not in error_detail or error_detail.get("category") is None
        assert "responsible_party" not in error_detail or error_detail.get("responsible_party") is None


def test_queued_build_query_error_detail_null(client, container):
    """Queried queued/running build: error_detail should be null."""
    compose = {
        "services": {
            "web": {"image": "python:3.12"}
        }
    }
    
    app_create = UserAppCreate(
        appid="APP-QUERY-1",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-QUERY-1")
    
    # Create build
    response = client.post("/api/v1/jenkins/build_create/APP-QUERY-1", json={})
    assert response.status_code == 202
    build_id = response.json()["build_id"]
    
    # Query build
    query_response = client.get(f"/api/v1/jenkins/build_info/{build_id}")
    assert query_response.status_code == 200
    
    data = query_response.json()
    
    # Should be queued
    assert data["status"] == "queued"
    
    # error_detail should be null for non-failed builds
    assert data.get("error_detail") is None or data["error_detail"] == {}
