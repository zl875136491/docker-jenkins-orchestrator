"""测试recognized白名单判断：只有已知组合才recognized=true"""
from test_error_contract_integration import client, container
from tests.route_check_fixtures import prepare_static_pass
from orchestrator.models import UserAppCreate
from orchestrator.services import BuildInputError


def test_unrecognized_code_subcode_is_unknown_and_not_recognized(client, container):
    """未知code/subcode组合：category=unknown_error, recognized=false"""
    # 准备基础app
    appid = "REVIEW-UNREC"
    container.applications.create_app(
        UserAppCreate(
            appid=appid,
            name="review",
            compose={"services": {"web": {"image": "alpine"}}},
            repository_url="https://example.invalid/repo.git",
            require_route="/health",
        )
    )
    prepare_static_pass(container, appid)
    
    # 注入未识别code/subcode
    def fail_with_unrecognized(*a, **k):
        raise BuildInputError(
            "opaque",
            error_detail={
                "code": "UNRECOGNIZED_NEW_CODE",
                "subcode": "UNRECOGNIZED_NEW_SUBCODE",
            },
        )
    
    import unittest.mock as mock
    with mock.patch.object(container.builds, "queue_build", fail_with_unrecognized):
        r = client.post(f"/api/v1/jenkins/build_create/{appid}", json={})
    
    assert r.status_code == 422
    data = r.json()
    # 未知组合必须保守分类
    assert data["error_detail"]["category"] == "unknown_error"
    assert data["error_detail"]["responsible_party"] == "unknown"
    assert data["error_detail"]["recognized"] is False
    assert data["error_detail"]["user_action_required"] is False
    # 保留原code/subcode字符串（兼容可接受但不可假识别）
    assert data["error_detail"]["code"] == "UNRECOGNIZED_NEW_CODE"
    assert data["error_detail"]["subcode"] == "UNRECOGNIZED_NEW_SUBCODE"


def test_recognized_code_with_unrecognized_subcode_is_also_unknown(client, container):
    """已知code但未知subcode：category=unknown_error, recognized=false"""
    appid = "REVIEW-PARTIAL"
    container.applications.create_app(
        UserAppCreate(
            appid=appid,
            name="review",
            compose={"services": {"web": {"image": "alpine"}}},
            repository_url="https://example.invalid/repo.git",
            require_route="/health",
        )
    )
    prepare_static_pass(container, appid)
    
    # COMPOSE_CONFIG_ERROR已知，但subcode未知
    def fail_with_partial(*a, **k):
        raise BuildInputError(
            "opaque",
            error_detail={
                "code": "COMPOSE_CONFIG_ERROR",
                "subcode": "UNRECOGNIZED_NEW_SUBCODE",
            },
        )
    
    import unittest.mock as mock
    with mock.patch.object(container.builds, "queue_build", fail_with_partial):
        r = client.post(f"/api/v1/jenkins/build_create/{appid}", json={})
    
    assert r.status_code == 422
    data = r.json()
    # 部分已知组合也必须保守分类
    assert data["error_detail"]["category"] == "unknown_error"
    assert data["error_detail"]["responsible_party"] == "unknown"
    assert data["error_detail"]["recognized"] is False
    assert data["error_detail"]["user_action_required"] is False
    assert data["error_detail"]["code"] == "COMPOSE_CONFIG_ERROR"
    assert data["error_detail"]["subcode"] == "UNRECOGNIZED_NEW_SUBCODE"
