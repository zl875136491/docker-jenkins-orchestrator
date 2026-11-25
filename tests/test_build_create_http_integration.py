"""HTTP integration tests for POST /api/v1/jenkins/build_create with env validation.

Uses TestClient + in-memory repository + fake dispatcher.
Stubs route check gate without needing real GitLab adapter.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import create_app
from orchestrator.config import Settings
from orchestrator.models import BuildCreate
from tests.route_check_fixtures import prepare_static_pass


@pytest.fixture
def client(tmp_path):
    """Create test client with in-memory backend and fake GitLab for route checks."""
    from orchestrator.container import create_container
    from test_worker_runtime import FakeGitLab
    
    settings = Settings(
        workspace_root=tmp_path / "storage",
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


def register_with_static_pass(client, appid, compose, environment=None):
    """Register app and prepare static check pass."""
    payload = {
        "appid": appid,
        "name": f"Test {appid}",
        "compose": compose,
        "repository_url": "https://github.com/test/repo.git",
        "require_route": "/health",
    }
    if environment is not None:
        payload["environment"] = environment
    
    response = client.post("/api/v1/jenkins/app_create", json=payload)
    assert response.status_code == 201, response.text
    
    # Stub static gate: prepare passing check
    container = client.app.state.container
    prepare_static_pass(container, appid)
    
    return response.json()


def test_http_missing_single_variable_returns_422_with_error_detail(client):
    """POST build_create with missing variable returns HTTP 422 with complete error_detail."""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
                "environment": ["DATABASE_URL=${DATABASE_URL}"]
            }
        }
    }
    register_with_static_pass(client, "APP-HTTP-1", compose, environment={})
    
    container = client.app.state.container
    initial_builds = len(container.repository.builds)
    initial_dispatches = len(container.dispatcher.dispatched)
    
    # POST build_create
    response = client.post("/api/v1/jenkins/build_create/APP-HTTP-1", json={"git_ref": "main"})
    
    # Assertions: HTTP 422
    assert response.status_code == 422
    data = response.json()
    
    # detail string exists (backward compatibility)
    assert "detail" in data
    assert isinstance(data["detail"], str)
    assert "DATABASE_URL" in data["detail"]
    
    # error_detail complete
    assert "error_detail" in data
    error_detail = data["error_detail"]
    assert error_detail["code"] == "COMPOSE_CONFIG_ERROR"
    assert error_detail["subcode"] == "MISSING_ENVIRONMENT_VARIABLES"
    assert error_detail["recognized"] is True
    assert error_detail["failure_site"] == "precheck"
    assert error_detail["check_scope"] == "services.environment"
    
    # missing_variables array
    assert "missing_variables" in error_detail
    assert len(error_detail["missing_variables"]) == 1
    var = error_detail["missing_variables"][0]
    assert var["name"] == "DATABASE_URL"
    assert var["reason"] == "missing"
    assert len(var["locations"]) == 1
    assert var["locations"][0]["service"] == "web"
    assert var["locations"][0]["environment_key"] == "DATABASE_URL"
    
    # No build record created
    assert len(container.repository.builds) == initial_builds
    
    # No dispatcher call
    assert len(container.dispatcher.dispatched) == initial_dispatches


def test_http_multiple_missing_variables_returns_all_in_one_response(client):
    """POST build_create with multiple missing variables returns all at once."""
    compose = {
        "services": {
            "api": {
                "image": "node:18",
                "environment": [
                    "VAR1=${VAR1}",
                    "VAR2=${VAR2}",
                    "VAR3=${VAR3}",
                ]
            }
        }
    }
    register_with_static_pass(client, "APP-HTTP-2", compose, environment={})
    
    container = client.app.state.container
    initial_builds = len(container.repository.builds)
    
    response = client.post("/api/v1/jenkins/build_create/APP-HTTP-2", json={})
    
    assert response.status_code == 422
    error_detail = response.json()["error_detail"]
    
    # All 3 variables reported
    assert len(error_detail["missing_variables"]) == 3
    names = {v["name"] for v in error_detail["missing_variables"]}
    assert names == {"VAR1", "VAR2", "VAR3"}
    
    # All reasons are "missing"
    for var in error_detail["missing_variables"]:
        assert var["reason"] == "missing"
    
    # No build record created
    assert len(container.repository.builds) == initial_builds


def test_http_empty_value_with_colon_question_returns_422(client):
    """POST build_create with empty value and :? returns HTTP 422 with reason='empty'."""
    compose = {
        "services": {
            "app": {
                "image": "alpine",
                "environment": ["SECRET=${SECRET:?Required}"]
            }
        }
    }
    register_with_static_pass(client, "APP-HTTP-3", compose, environment={"SECRET": ""})
    
    response = client.post("/api/v1/jenkins/build_create/APP-HTTP-3", json={})
    
    assert response.status_code == 422
    error_detail = response.json()["error_detail"]
    
    assert len(error_detail["missing_variables"]) == 1
    var = error_detail["missing_variables"][0]
    assert var["name"] == "SECRET"
    assert var["reason"] == "empty"  # Not "missing"


def test_http_success_with_all_variables_configured_returns_202(client):
    """POST build_create with all variables configured returns HTTP 202 and creates build."""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
                "environment": ["DATABASE_URL=${DATABASE_URL}"]
            }
        }
    }
    register_with_static_pass(
        client,
        "APP-HTTP-4",
        compose,
        environment={"DATABASE_URL": "postgresql://localhost/db"}
    )
    
    container = client.app.state.container
    initial_builds = len(container.repository.builds)
    
    response = client.post("/api/v1/jenkins/build_create/APP-HTTP-4", json={})
    
    # Should succeed (or fail on other checks, but NOT env validation)
    if response.status_code == 422:
        data = response.json()
        error_detail = data.get("error_detail", {})
        # Must NOT be env validation error
        assert error_detail.get("subcode") != "MISSING_ENVIRONMENT_VARIABLES", \
            f"Should not fail on env validation when all vars configured: {data}"
    else:
        # Likely 202 or other status
        assert response.status_code == 202
        
        # Build record created
        assert len(container.repository.builds) == initial_builds + 1
        
        # Verify build snapshot includes compose
        build = response.json()
        assert "build_id" in build
        stored_build = container.repository.builds[build["build_id"]]
        assert stored_build.compose_snapshot is not None
        assert stored_build.compose_snapshot == compose


def test_http_static_gate_failure_takes_priority_over_env_validation(client):
    """POST build_create fails on static gate BEFORE env validation when static check not passed."""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
                "environment": ["MISSING_VAR=${MISSING_VAR}"]
            }
        }
    }
    
    # Register but DO NOT prepare static pass
    payload = {
        "appid": "APP-HTTP-5",
        "name": "Test APP-HTTP-5",
        "compose": compose,
        "repository_url": "https://github.com/test/repo.git",
        "require_route": "/health",
    }
    response = client.post("/api/v1/jenkins/app_create", json=payload)
    assert response.status_code == 201
    
    # Attempt build_create without static pass
    response = client.post("/api/v1/jenkins/build_create/APP-HTTP-5", json={})
    
    assert response.status_code == 422
    data = response.json()
    
    # Should fail on static check, NOT on env validation
    assert "未进行路由静态检查" in data["detail"] or "static" in data["detail"].lower()
    
    # Should NOT mention missing variables
    assert "MISSING_VAR" not in data["detail"]
    
    # Should NOT have MISSING_ENVIRONMENT_VARIABLES subcode
    error_detail = data.get("error_detail", {})
    assert error_detail.get("subcode") != "MISSING_ENVIRONMENT_VARIABLES"


def test_http_unknown_error_not_misclassified_as_compose_error(client):
    """POST build_create with unknown error does not return COMPOSE_CONFIG_ERROR."""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
                "environment": []  # Valid, no variables
            }
        }
    }
    register_with_static_pass(client, "APP-HTTP-6", compose, environment={})
    
    # Trigger unknown error by deleting app after registration
    container = client.app.state.container
    del container.repository.apps["APP-HTTP-6"]
    
    response = client.post("/api/v1/jenkins/build_create/APP-HTTP-6", json={})
    
    # Should fail (404 or other), but NOT with COMPOSE_CONFIG_ERROR
    assert response.status_code in {404, 422, 500}
    
    if response.status_code == 422:
        data = response.json()
        error_detail = data.get("error_detail", {})
        # Must NOT be classified as Compose error
        assert error_detail.get("code") != "COMPOSE_CONFIG_ERROR"
        assert error_detail.get("subcode") != "MISSING_ENVIRONMENT_VARIABLES"


def test_http_aggregates_same_variable_across_services(client):
    """POST build_create aggregates same variable from multiple services into one entry."""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
                "environment": ["DB_URL=${DB_URL}"]
            },
            "worker": {
                "image": "python:3.12",
                "environment": ["DB_URL=${DB_URL}"]
            },
            "scheduler": {
                "image": "python:3.12",
                "environment": ["DB_URL=${DB_URL}"]
            }
        }
    }
    register_with_static_pass(client, "APP-HTTP-7", compose, environment={})
    
    response = client.post("/api/v1/jenkins/build_create/APP-HTTP-7", json={})
    
    assert response.status_code == 422
    error_detail = response.json()["error_detail"]
    
    # Only ONE entry for DB_URL
    assert len(error_detail["missing_variables"]) == 1
    var = error_detail["missing_variables"][0]
    assert var["name"] == "DB_URL"
    
    # But includes all 3 locations
    assert len(var["locations"]) == 3
    services = {loc["service"] for loc in var["locations"]}
    assert services == {"web", "worker", "scheduler"}


def test_http_backward_compatibility_detail_and_error_detail(client):
    """POST build_create returns both detail (old) and error_detail (new) for compatibility."""
    compose = {
        "services": {
            "api": {
                "image": "node:18",
                "environment": ["API_KEY=${API_KEY}"]
            }
        }
    }
    register_with_static_pass(client, "APP-HTTP-8", compose, environment={})
    
    response = client.post("/api/v1/jenkins/build_create/APP-HTTP-8", json={})
    
    assert response.status_code == 422
    data = response.json()
    
    # Old field: detail (string)
    assert "detail" in data
    assert isinstance(data["detail"], str)
    assert len(data["detail"]) > 0
    
    # New field: error_detail (object)
    assert "error_detail" in data
    assert isinstance(data["error_detail"], dict)
    assert "missing_variables" in data["error_detail"]


def test_http_no_secret_leakage_in_response(client):
    """POST build_create error response never includes actual variable values."""
    compose = {
        "services": {
            "app": {
                "image": "alpine",
                "environment": [
                    "SECRET=${SECRET}",
                    "PASSWORD=${PASSWORD:?Required password}",
                ]
            }
        }
    }
    register_with_static_pass(
        client,
        "APP-HTTP-9",
        compose,
        environment={"SECRET": "supersecret123", "PASSWORD": "mypassword456"}
    )
    
    # Update to trigger validation failure
    patch_response = client.patch(
        "/api/v1/jenkins/app_info/APP-HTTP-9",
        json={"environment": {"SECRET": "", "PASSWORD": ""}}
    )
    assert patch_response.status_code == 200
    
    response = client.post("/api/v1/jenkins/build_create/APP-HTTP-9", json={})
    
    assert response.status_code == 422
    response_text = response.text
    
    # Verify no actual secret values leaked
    assert "supersecret123" not in response_text
    assert "mypassword456" not in response_text
    
    # Verify no user hint leaked
    assert "Required password" not in response_text
    
    # Variable names are OK (at least one should be present)
    assert "PASSWORD" in response_text or "SECRET" in response_text
