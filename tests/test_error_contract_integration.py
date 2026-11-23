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
            workspace_download_base_url="http://testserver"
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


def test_http_external_stateful_endpoint_真实触发_returns_422_with_classification(client, container):
    """HTTP POST with external stateful endpoint returns 422 with complete error classification."""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
                "environment": {
                    "DATABASE_URL": "postgres://external.db.com:5432/prod"
                }
            }
        }
    }
    
    # Create app
    app_create = UserAppCreate(
        appid="APP-ERROR-CONTRACT-1",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-ERROR-CONTRACT-1")
    
    # Try to create build (should fail on external stateful endpoint validation)
    response = client.post("/api/v1/jenkins/build_create/APP-ERROR-CONTRACT-1", json={})
    
    # Should return 422 (not 202)
    assert response.status_code == 422
    
    data = response.json()
    
    # Should have detail string
    assert "detail" in data
    assert isinstance(data["detail"], str)
    assert len(data["detail"]) > 0
    
    # Should have error_detail dict
    assert "error_detail" in data
    error_detail = data["error_detail"]
    assert isinstance(error_detail, dict)
    
    # Should have classification fields
    assert "category" in error_detail
    assert error_detail["category"] == CONFIG_ERROR
    
    assert "responsible_party" in error_detail
    assert error_detail["responsible_party"] == APP_DEV
    
    assert "retryable" in error_detail
    assert error_detail["retryable"] is False
    
    assert "user_action_required" in error_detail
    assert error_detail["user_action_required"] is True
    
    assert "action_guidance" in error_detail
    assert isinstance(error_detail["action_guidance"], str)
    assert "数据库服务" in error_detail["action_guidance"] or "服务名" in error_detail["action_guidance"]
    
    # Should have summary in Chinese
    assert "summary" in error_detail
    assert isinstance(error_detail["summary"], str)
    
    # Should be recognized
    assert "recognized" in error_detail
    assert error_detail["recognized"] is True
    
    # Should have failure_site
    assert "failure_site" in error_detail
    assert error_detail["failure_site"] in {"precheck", "validating"}
    
    # Build should NOT be created (422 means validation failed before build creation)
    builds = container.repository.list_builds("APP-ERROR-CONTRACT-1")
    assert len(builds) == 0
    
    # Should have explicit code/subcode from source
    assert error_detail.get("code") == "COMPOSE_CONFIG_ERROR"
    assert error_detail.get("subcode") == "EXTERNAL_STATEFUL_ENDPOINT"
    
    # Should have endpoint_variable for diagnosis
    assert "endpoint_variable" in error_detail
    assert error_detail["endpoint_variable"] == "DATABASE_URL"


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


def test_worker_external_stateful_endpoint_真实门禁_persists_classification(container):
    """Worker validating stage persists external stateful endpoint error with classification."""
    compose = {
        "services": {
            "api": {
                "image": "node:18",
                "environment": {
                    "REDIS_URL": "redis://external.cache.com:6379"
                }
            }
        }
    }
    
    app_create = UserAppCreate(
        appid="APP-WORKER-ERROR-1",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-WORKER-ERROR-1")
    
    # Queue build (bypass sync gate for testing worker path)
    build = container.builds.queue_build("APP-WORKER-ERROR-1", BuildCreate())
    
    # Run worker (will fail on external stateful endpoint validation)
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
    
    result = runtime.start_build(build.build_id, "task")
    
    # Should fail
    assert result["status"] == "failed"
    
    # Check persisted build
    failed_build = container.builds.get_build(build.build_id)
    assert failed_build.status.value == "failed"
    assert failed_build.failure_site == "validating"
    
    # Should have error_detail with explicit code/subcode and classification
    assert failed_build.error_detail is not None
    error_detail = failed_build.error_detail
    
    # Explicit code/subcode from source
    assert error_detail.get("code") == "COMPOSE_CONFIG_ERROR"
    assert error_detail.get("subcode") == "EXTERNAL_STATEFUL_ENDPOINT"
    
    # Classification
    assert error_detail["category"] == CONFIG_ERROR
    assert error_detail["responsible_party"] == APP_DEV
    assert error_detail["retryable"] is False
    assert error_detail["user_action_required"] is True
    assert "action_guidance" in error_detail


def test_worker_unknown_error_classified_conservatively(container):
    """Worker unknown error is classified conservatively (not fake责任)."""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
            }
        }
    }
    
    app_create = UserAppCreate(
        appid="APP-WORKER-UNKNOWN-1",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-WORKER-UNKNOWN-1")
    
    build = container.builds.queue_build("APP-WORKER-UNKNOWN-1", BuildCreate())
    
    # Simulate unknown error by corrupting build
    container.repository.update_build(build.build_id, {"git_commit_sha": None})
    
    jenkins = FakeJenkins([], build=None)
    gitlab = FakeGitLab("b" * 40)
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
    
    # Unknown error should NOT be classified as user's responsibility
    assert error_detail["category"] == UNKNOWN_ERROR
    assert error_detail["responsible_party"] == UNKNOWN_PARTY
    assert error_detail["recognized"] is False
    assert "联系平台排查" in error_detail.get("action_guidance", "")


def test_success_202_does_not_have_error_classification(client, container):
    """Successful 202 response does not包含 error classification fields."""
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
    
    # Should NOT have error fields
    assert "error_detail" not in data
    assert "category" not in data
    assert "responsible_party" not in data
    
    # Should have build fields
    assert "build_id" in data
    assert "status" in data


def test_queued_build_query_does_not_包含_error_fields(client, container):
    """Queried queued/running build does not包含 error fields."""
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
    
    # Should NOT have error fields (not failed)
    assert data["status"] == "queued"
    if "error_detail" in data and data["error_detail"] is not None:
        # If error_detail exists (shouldn't for queued), it should not包含 false error classification
        assert "category" not in data["error_detail"] or data["error_detail"]["category"] == ""
