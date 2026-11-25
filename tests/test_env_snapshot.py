"""Tests for environment variable snapshot isolation.

Verifies that worker uses build_resources.environment_ciphertext snapshot,
not current app.environment_ciphertext, so PATCH app after build creation
does not affect in-flight builds.
"""

from pathlib import Path
import tempfile

import pytest

from orchestrator.config import Settings
from orchestrator.container import create_container
from orchestrator.models import BuildCreate, UserAppCreate, UserAppUpdate
from orchestrator.services import BuildInputError
from orchestrator.worker_runtime import WorkerRuntime
from test_worker_runtime import FakeJenkins, FakeDockerServices, FakeGitLab
from tests.route_check_fixtures import prepare_static_pass


@pytest.fixture
def container():
    """Create container with in-memory repository."""
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            workspace_root=Path(tmp) / "storage",
            workspace_host_root=Path("/srv/workspaces"),
        )
        container = create_container(settings)
        fake_gitlab = FakeGitLab("a" * 40)
        container.route_checks.gitlab = lambda: fake_gitlab
        yield container


def test_worker_uses_environment_snapshot_not_current_app(container):
    """Worker validating uses build_resources.environment_ciphertext snapshot, not app.environment_ciphertext."""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
                "environment": ["REQUIRED_VAR=${REQUIRED_VAR}"]
            }
        }
    }
    
    # Create app with variable configured
    app_create = UserAppCreate(
        appid="APP-SNAPSHOT-1",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={"REQUIRED_VAR": "initial_value"},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-SNAPSHOT-1")
    
    # Queue build (creates environment snapshot in build_resources)
    build = container.builds.queue_build("APP-SNAPSHOT-1", BuildCreate())
    
    # Verify build_resources has snapshot
    build_resources = container.repository.get_build_resources(build.build_id)
    assert build_resources is not None
    assert build_resources.environment_ciphertext is not None
    snapshot_env = container.applications.secret_box.decrypt_environment(build_resources.environment_ciphertext)
    assert snapshot_env == {"REQUIRED_VAR": "initial_value"}
    
    # PATCH app to remove variable (simulate user error)
    container.applications.update_app("APP-SNAPSHOT-1", UserAppUpdate(environment={}))
    
    # Verify app now has no variables
    updated_app = container.applications.get_record("APP-SNAPSHOT-1")
    current_env = container.applications.secret_box.decrypt_environment(updated_app.environment_ciphertext)
    assert current_env == {}
    
    # Run worker - should use snapshot, NOT current app
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
    
    # Should succeed (uses snapshot with REQUIRED_VAR="initial_value")
    # Will fail on other checks (no Jenkins build), but NOT on env validation
    failed_build = container.builds.get_build(build.build_id)
    
    # If it failed on env validation, it would be at validating stage
    # and error would mention REQUIRED_VAR
    if failed_build.status.value == "failed" and failed_build.failure_site == "validating":
        error_str = str(failed_build.error or "")
        error_detail_str = str(failed_build.error_detail or "")
        # Should NOT mention REQUIRED_VAR (because snapshot has it)
        assert "REQUIRED_VAR" not in error_str, f"Worker should use snapshot, not current app: {error_str}"
        assert "REQUIRED_VAR" not in error_detail_str, f"Worker should use snapshot, not current app: {error_detail_str}"


def test_patch_app_environment_after_build_does_not_affect_in_flight(container):
    """PATCH app.environment after build creation does not affect in-flight build."""
    compose = {
        "services": {
            "api": {
                "image": "node:18",
                "environment": [
                    "VAR1=${VAR1}",
                    "VAR2=${VAR2}",
                ]
            }
        }
    }
    
    # Create app with both variables
    app_create = UserAppCreate(
        appid="APP-SNAPSHOT-2",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={"VAR1": "value1", "VAR2": "value2"},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-SNAPSHOT-2")
    
    # Queue first build
    build1 = container.builds.queue_build("APP-SNAPSHOT-2", BuildCreate())
    
    # PATCH app to remove VAR2
    container.applications.update_app("APP-SNAPSHOT-2", UserAppUpdate(
        environment={"VAR1": "value1"}  # VAR2 removed
    ))
    
    # Queue second build (should use updated environment without VAR2)
    prepare_static_pass(container, "APP-SNAPSHOT-2", commit_sha="b" * 40)
    
    # Second build should fail on sync gate (VAR2 missing)
    try:
        build2 = container.builds.queue_build("APP-SNAPSHOT-2", BuildCreate())
        assert False, "Second build should fail on sync gate due to missing VAR2"
    except BuildInputError as e:
        # Expected: sync gate catches missing VAR2
        # Check error_detail for VAR2
        assert e.error_detail is not None
        missing_vars = e.error_detail.get("missing_variables", [])
        if isinstance(missing_vars, list):
            var_names = []
            for v in missing_vars:
                if isinstance(v, dict):
                    var_names.append(v.get("name") or v.get("variable", ""))
                elif isinstance(v, str):
                    var_names.append(v)
            assert "VAR2" in var_names, f"Expected VAR2 in missing variables, got {var_names}"
        else:
            # Fallback to summary
            assert "VAR2" in str(e.error_detail.get("summary", "")), f"Expected VAR2 in summary"
    
    # First build should still work with snapshot
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
    
    result = runtime.start_build(build1.build_id, "task")
    
    # Should not fail on VAR2 (snapshot has it)
    failed_build = container.builds.get_build(build1.build_id)
    if failed_build.failure_site == "validating":
        error_detail = failed_build.error_detail or {}
        if error_detail.get("subcode") == "MISSING_ENVIRONMENT_VARIABLES":
            # Should not report VAR2 as missing
            missing_names = {v["name"] for v in error_detail.get("missing_variables", [])}
            assert "VAR2" not in missing_names, "First build should use snapshot with VAR2"


def test_worker_aggregates_multiple_missing_variables_in_validating(container):
    """Worker validating stage reports all missing variables at once (async defense)."""
    compose = {
        "services": {
            "web": {
                "image": "python:3.12",
                "environment": [
                    "VAR1=${VAR1}",
                    "VAR2=${VAR2}",
                    "VAR3=${VAR3}",
                ]
            }
        }
    }
    
    # Create app with all variables
    app_create = UserAppCreate(
        appid="APP-WORKER-ASYNC-1",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={"VAR1": "v1", "VAR2": "v2", "VAR3": "v3"},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-WORKER-ASYNC-1")
    
    # Queue build
    build = container.builds.queue_build("APP-WORKER-ASYNC-1", BuildCreate())
    
    # Corrupt environment snapshot to simulate async drift
    # (In reality this shouldn't happen, but tests async defense)
    build_resources = container.repository.get_build_resources(build.build_id)
    corrupted_env = container.applications.secret_box.encrypt_environment({})
    container.repository.update_build_resources(build.build_id, {
        "environment_ciphertext": corrupted_env
    })
    
    # Run worker
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
    
    # Should fail with all 3 variables reported
    assert result["status"] == "failed"
    
    failed_build = container.builds.get_build(build.build_id)
    assert failed_build.failure_site == "validating"
    assert failed_build.error_detail is not None
    assert failed_build.error_detail["subcode"] == "MISSING_ENVIRONMENT_VARIABLES"
    
    # All 3 variables should be in one error
    assert len(failed_build.error_detail["missing_variables"]) == 3
    names = {v["name"] for v in failed_build.error_detail["missing_variables"]}
    assert names == {"VAR1", "VAR2", "VAR3"}
    
    # Each should have locations
    for var in failed_build.error_detail["missing_variables"]:
        assert len(var["locations"]) > 0
        assert var["reason"] == "missing"


def test_sync_and_async_use_same_input_snapshot(container):
    """Sync gate and async worker use same compose + environment snapshot."""
    compose = {
        "services": {
            "app": {
                "image": "alpine",
                "environment": ["CHECK_VAR=${CHECK_VAR}"]
            }
        }
    }
    
    # Create app with variable
    app_create = UserAppCreate(
        appid="APP-SYNC-ASYNC-1",
        name="Test App",
        compose=compose,
        repository_url="https://github.com/test/repo.git",
        require_route="/health",
        environment={"CHECK_VAR": "test_value"},
    )
    container.applications.create_app(app_create)
    prepare_static_pass(container, "APP-SYNC-ASYNC-1")
    
    # Queue build (sync gate passes)
    build = container.builds.queue_build("APP-SYNC-ASYNC-1", BuildCreate())
    
    # Verify snapshots
    assert build.compose_snapshot == compose
    build_resources = container.repository.get_build_resources(build.build_id)
    snapshot_env = container.applications.secret_box.decrypt_environment(build_resources.environment_ciphertext)
    assert snapshot_env == {"CHECK_VAR": "test_value"}
    
    # Worker will use same snapshots (verified by other tests)
    # This test just confirms snapshot creation order:
    # 1. queue_build validates app.compose + app.environment
    # 2. queue_build creates build with compose_snapshot
    # 3. queue_build creates build_resources with environment_ciphertext
    # No race condition between sync gate and snapshot creation
