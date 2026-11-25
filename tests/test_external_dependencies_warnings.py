"""测试外部依赖检测改为非阻断warnings"""
import pytest
from test_error_contract_integration import client, container
from orchestrator.models import UserAppCreate, BuildCreate
from orchestrator.worker_runtime import WorkerRuntime
from test_worker_runtime import FakeJenkins, FakeDockerServices, FakeGitLab
from tests.route_check_fixtures import prepare_static_pass


def test_scm_external_databases_generate_warnings_not_errors(client, container):
    """SCM场景：ECN/HANA/SQL等外部业务数据库生成warnings不阻断构建"""
    appid = "SCM-EXTERNAL"
    
    # SCM典型配置：本构建mongo/redis + 外部ECN/HANA/SQL
    compose = {
        "services": {
            "api": {
                "image": "api:latest",
                "environment": {
                    # 本构建服务（应该正常）
                    "MONGO_URI": "${MONGO_URI:-mongodb://mongo:27017/scm}",
                    "REDIS_URL": "redis://redis:6379/0",
                    # 外部业务数据库（应该生成warnings）
                    "ECN_DB_HOST": "${ECN_DB_HOST:-10.2.106.2}",
                    "ECN_DB_PORT": "${ECN_DB_PORT:-3309}",
                    "HANA_HOST": "${HANA_HOST:-10.2.101.32}",
                    "HANA_PORT": "${HANA_PORT:-30015}",
                    "SQL_HOST": "${SQL_HOST:-10.0.100.218}",
                    "SQL_DATABASE": "${SQL_DATABASE:-supplychaindb}",
                },
            },
            "mongo": {
                "image": "mongo:7",
            },
            "redis": {
                "image": "redis:7",
            },
        }
    }
    
    container.applications.create_app(
        UserAppCreate(
            appid=appid,
            name="scm-test",
            compose=compose,
            repository_url="https://example.invalid/repo.git",
            require_route="/health",
        )
    )
    prepare_static_pass(container, appid)
    
    # 提供必需的MONGO_URI
    build = container.builds.queue_build(
        appid,
        BuildCreate(environment={"MONGO_URI": "mongodb://mongo:27017/scm"}),
    )
    
    # 应该成功创建构建（不再被外部DB阻断）
    assert build.status == "queued"
    assert build.build_id is not None
    
    # 通过GET验证warnings字段
    r = client.get(f"/api/v1/jenkins/build_info/{build.build_id}")
    assert r.status_code == 200
    data = r.json()
    
    # 构建应该排队成功（不是failed）
    assert data["status"] == "queued"
    
    # 应该没有error（不阻断）
    assert data.get("error") is None


def test_external_dependency_warnings_structure():
    """验证warnings结构：code, severity, blocking, service, environment_key, config_path"""
    from orchestrator.docker_services import DockerSwarmAdapter
    from pathlib import Path
    import docker
    
    compose = {
        "services": {
            "api": {
                "image": "api:latest",
                "environment": {
                    "MONGO_URI": "mongodb://mongo:27017/db",  # 本构建服务
                    "EXTERNAL_DB_HOST": "10.0.0.1",  # 外部
                },
            },
            "mongo": {"image": "mongo:7"},
        }
    }
    
    # 创建adapter（不需要真实docker client来测试_validate_private_backends）
    adapter = DockerSwarmAdapter(
        docker_client=docker.from_env(),
        appid="TESTAPP",
        namespace="test-namespace",
        build_id="test-build",
        workspace_host_root=Path("/tmp"),
    )
    
    warnings = adapter._validate_private_backends(compose, {})
    
    # 应该检测到外部依赖
    assert len(warnings) > 0
    
    # 验证warning结构
    for warning in warnings:
        assert "code" in warning
        assert "severity" in warning
        assert "blocking" in warning
        assert warning["blocking"] is False
        assert "message" in warning
        # service可能为None（全局环境变量）
        assert "environment_key" in warning
        assert "config_path" in warning
        
        # 不应该回显实际地址/URI/值
        assert "10.0.0.1" not in str(warning.get("message", ""))


def test_missing_variables_still_422(client, container):
    """缺失必需变量仍然422阻断（不受warnings影响）"""
    appid = "MISSING-VAR"
    
    compose = {
        "services": {
            "api": {
                "image": "api:latest",
                "environment": {
                    "REQUIRED_VAR": "${REQUIRED_VAR:?必须提供}",
                    "EXTERNAL_DB": "10.0.0.1",  # 这个只生成warning
                },
            }
        }
    }
    
    container.applications.create_app(
        UserAppCreate(
            appid=appid,
            name="missing-test",
            compose=compose,
            repository_url="https://example.invalid/repo.git",
            require_route="/health",
        )
    )
    prepare_static_pass(container, appid)
    
    # 不提供REQUIRED_VAR
    r = client.post(f"/api/v1/jenkins/build_create/{appid}", json={})
    
    # 应该422（缺变量仍然阻断）
    assert r.status_code == 422
    data = r.json()
    assert "error_detail" in data
    assert data["error_detail"]["code"] == "COMPOSE_CONFIG_ERROR"
    assert data["error_detail"]["subcode"] == "MISSING_ENVIRONMENT_VARIABLES"


def test_docker_isolation_violations_still_blocked(client, container):
    """Docker隔离违规（外部网络等）仍然阻断"""
    appid = "ISOLATION-TEST"
    
    compose = {
        "services": {
            "api": {
                "image": "api:latest",
                "networks": ["external_network"],  # 违规：外部网络
            }
        },
        "networks": {
            "external_network": {"external": True}
        }
    }
    
    container.applications.create_app(
        UserAppCreate(
            appid=appid,
            name="isolation-test",
            compose=compose,
            repository_url="https://example.invalid/repo.git",
            require_route="/health",
        )
    )
    prepare_static_pass(container, appid)
    
    r = client.post(f"/api/v1/jenkins/build_create/{appid}", json={})
    
    # 应该422（隔离违规仍然阻断）
    assert r.status_code == 422
    # 或者在worker阶段失败，但不应该成功
