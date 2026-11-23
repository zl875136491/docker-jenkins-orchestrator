"""全面安全测试：验证各种secret不泄漏场景

测试随机secret、中文+secret、query token、error_detail注入、BuildService.fail等
不依赖FAKE_特征词，使用真实随机值
"""
import json
import random
import string
import pytest
from orchestrator.error_classification import enrich_error_detail, get_safe_summary_from_template
from orchestrator.services import BuildInputError
from orchestrator.models import UserAppCreate, BuildCreate
from orchestrator.worker_runtime import WorkerRuntime
from tests.test_worker_runtime import FakeJenkins, FakeDockerServices, FakeGitLab
from tests.route_check_fixtures import prepare_static_pass
from tests.test_error_contract_integration import client, container


def random_secret(length=16):
    """生成随机secret（模拟真实密码）"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


def test_short_random_token_not_echoed(client, container, monkeypatch):
    """短字符串token=任意随机值，不应回显"""
    secret = random_secret(12)
    
    def fail(*a, **k):
        raise BuildInputError(f"auth token={secret}")
    
    monkeypatch.setattr(container.builds, "queue_build", fail)
    response = client.post("/api/v1/jenkins/build_create/REVIEW", json={})
    
    assert response.status_code == 422
    assert secret not in response.text


def test_chinese_message_with_random_secret_not_echoed(client, container, monkeypatch):
    """包含中文+随机secret，不应回显secret"""
    secret = random_secret(20)
    
    def fail(*a, **k):
        raise BuildInputError(f"数据库连接失败，密码：{secret}")
    
    monkeypatch.setattr(container.builds, "queue_build", fail)
    response = client.post("/api/v1/jenkins/build_create/REVIEW", json={})
    
    assert response.status_code == 422
    assert secret not in response.text
    # 应该有中文通用摘要
    data = response.json()
    assert "detail" in data


def test_uri_with_query_token_not_echoed(client, container, monkeypatch):
    """URI无密码段但query参数含token，不应回显"""
    token = random_secret(32)
    
    def fail(*a, **k):
        raise BuildInputError(f"failed to fetch https://api.example.com/data?token={token}")
    
    monkeypatch.setattr(container.builds, "queue_build", fail)
    response = client.post("/api/v1/jenkins/build_create/REVIEW", json={})
    
    assert response.status_code == 422
    assert token not in response.text


def test_error_detail_summary_injection_not_echoed(client, container, monkeypatch):
    """exc.error_detail.summary注入随机secret，不应原样返回"""
    secret = random_secret(24)
    
    def fail(*a, **k):
        exc = BuildInputError("validation error")
        exc.error_detail = {
            "summary": f"Internal error: credential={secret}",
            "failure_site": "precheck",
        }
        raise exc
    
    monkeypatch.setattr(container.builds, "queue_build", fail)
    response = client.post("/api/v1/jenkins/build_create/REVIEW", json={})
    
    assert response.status_code == 422
    assert secret not in response.text


def test_error_detail_remediation_injection_not_echoed(client, container, monkeypatch):
    """exc.error_detail.remediation注入secret，不应泄漏"""
    secret = random_secret(16)
    
    def fail(*a, **k):
        exc = BuildInputError("config error")
        exc.error_detail = {
            "remediation": f"Use password: {secret}",
            "failure_site": "precheck",
        }
        raise exc
    
    monkeypatch.setattr(container.builds, "queue_build", fail)
    response = client.post("/api/v1/jenkins/build_create/REVIEW", json={})
    
    assert response.status_code == 422
    assert secret not in response.text


def test_build_service_fail_message_injection_not_echoed(client, container):
    """直接BuildService.fail(message)输入随机secret，不应回显"""
    secret = random_secret(18)
    
    container.applications.create_app(UserAppCreate(
        appid="REVIEW-FAIL-MSG",
        name="review",
        compose={"services": {"web": {"image": "alpine"}}},
        repository_url="https://example.invalid/repo.git",
        require_route="/health"
    ))
    prepare_static_pass(container, "REVIEW-FAIL-MSG")
    
    build = container.builds.queue_build("REVIEW-FAIL-MSG", BuildCreate())
    
    # 模拟worker直接调用fail并传入含secret的message
    from orchestrator.docker_services import DockerServiceError
    exc = DockerServiceError(f"deployment failed with key={secret}")
    
    rt = WorkerRuntime(
        container.settings,
        repository=container.repository,
        build_service=container.builds,
        catalog=container.catalog,
        jenkins=FakeJenkins([], build=None),
        gitlab=FakeGitLab("a" * 40),
        docker_services=FakeDockerServices(None),
        poll_scheduler=lambda *a: None,
        route_checks=container.route_checks,
        dynamic_checks=None
    )
    
    class BadAdapter:
        def validate_build_compose(self, *a, **k):
            raise exc
    
    import unittest.mock
    with unittest.mock.patch.object(rt, "_docker_validation_adapter", return_value=BadAdapter()):
        rt.start_build(build.build_id, "review-task")
    
    response = client.get(f"/api/v1/jenkins/build_info/{build.build_id}")
    
    assert secret not in response.text


def test_external_endpoint_keeps_useful_chinese_guidance():
    """外部endpoint已识别错误，应保持有用中文建议（不含secret）"""
    error_detail = {
        "code": "COMPOSE_CONFIG_ERROR",
        "subcode": "EXTERNAL_STATEFUL_ENDPOINT",
        "endpoint_variable": "DATABASE_URL",
        "failure_site": "validating",
    }
    
    enriched = enrich_error_detail(error_detail)
    
    # 应该有明确的中文建议
    assert "summary" in enriched
    assert "DATABASE_URL" in enriched["summary"]
    assert "外部" in enriched["summary"] or "隔离" in enriched["summary"]
    
    # 应该有action_guidance
    assert "action_guidance" in enriched
    assert "数据库服务" in enriched["action_guidance"] or "服务名" in enriched["action_guidance"]


def test_missing_variables_keeps_useful_chinese_guidance():
    """缺变量已识别错误，应保持有用中文建议"""
    error_detail = {
        "code": "COMPOSE_CONFIG_ERROR",
        "subcode": "MISSING_ENVIRONMENT_VARIABLES",
        "missing_variables": ["API_KEY", "DB_HOST"],
        "failure_site": "validating",
    }
    
    enriched = enrich_error_detail(error_detail)
    
    # 应该列出缺失变量名（白名单）
    assert "summary" in enriched
    assert "API_KEY" in enriched["summary"]
    assert "DB_HOST" in enriched["summary"]


def test_template_generation_whitelist_only():
    """模板生成仅使用白名单元数据"""
    # 有效白名单：变量名
    summary1 = get_safe_summary_from_template(
        "COMPOSE_CONFIG_ERROR",
        "EXTERNAL_STATEFUL_ENDPOINT",
        {"endpoint_variable": "MONGO_URI"}
    )
    assert "MONGO_URI" in summary1
    
    # 无白名单元数据
    summary2 = get_safe_summary_from_template(
        "COMPOSE_CONFIG_ERROR",
        "EXTERNAL_STATEFUL_ENDPOINT",
        {}
    )
    assert "外部" in summary2 or "端点" in summary2
    
    # 未知code/subcode
    summary3 = get_safe_summary_from_template(None, None, {})
    assert "构建失败" in summary3 or "排查" in summary3
