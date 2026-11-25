import json
import pytest
from orchestrator.error_classification import classify_error
from orchestrator.services import BuildInputError
from orchestrator.models import UserAppCreate, BuildCreate
from orchestrator.worker_runtime import WorkerRuntime
from tests.test_worker_runtime import FakeJenkins, FakeDockerServices, FakeGitLab
from tests.route_check_fixtures import prepare_static_pass
from tests.test_error_contract_integration import client, container

def test_real_worker_external_endpoint_does_not_leak_password(client, container):
    """新行为：外部端点生成warnings不阻断，但仍不泄漏密码"""
    container.applications.create_app(UserAppCreate(appid="REVIEW-ENDPOINT",name="review",compose={"services":{"web":{"image":"alpine","environment":{"MONGO_URI":"${MONGO_URI:-mongodb://mongo:27017/db}"}}}},repository_url="https://example.invalid/repo.git",require_route="/health",environment={"MONGO_URI":"mongodb://fakeuser:FAKE_PASSWORD_123@external.invalid/db"}))
    prepare_static_pass(container,"REVIEW-ENDPOINT")
    b=container.builds.queue_build("REVIEW-ENDPOINT", BuildCreate())
    rt=WorkerRuntime(container.settings,repository=container.repository,build_service=container.builds,catalog=container.catalog,jenkins=FakeJenkins([],build=None),gitlab=FakeGitLab("a"*40),docker_services=FakeDockerServices(None),poll_scheduler=lambda *a:None,route_checks=container.route_checks,dynamic_checks=None)
    result=rt.start_build(b.build_id,"review-task")
    response=client.get("/api/v1/jenkins/build_info/"+b.build_id)
    data=response.json()
    print("WORKER_PUBLIC_RESULT",json.dumps({k:data.get(k) for k in ("status","error","error_detail","validation_warnings","failure_logs","raw_log_excerpt")},ensure_ascii=False))
    # 新行为：不应该因为外部端点失败，但密码仍不泄漏
    assert "FAKE_PASSWORD_123" not in response.text
    # 新行为：外部端点不再导致status=failed
    # （可能因其他原因如没有真实Jenkins而失败，但不是因为EXTERNAL_STATEFUL_ENDPOINT）
    if data.get("error_detail"):
        assert data["error_detail"].get("subcode") != "EXTERNAL_STATEFUL_ENDPOINT"

def test_generic_build_input_is_not_known_configuration():
    d=classify_error(exception=BuildInputError("Unable to enqueue build task"),failure_site="precheck")
    print("GENERIC_CLASSIFICATION",d)
    assert d["category"]=="unknown_error"

def test_public_sync_error_does_not_echo_exception(client,container,monkeypatch):
    def fail(*a,**k):
        raise BuildInputError("upstream URI mongodb://review:FAKE_SENTINEL_789@external.invalid/db")
    monkeypatch.setattr(container.builds,"queue_build",fail)
    response=client.post("/api/v1/jenkins/build_create/REVIEW",json={})
    print("SYNC_PUBLIC_RESULT",response.text)
    assert response.status_code==422
    assert "FAKE_SENTINEL_789" not in response.text

def test_async_failure_does_not_echo_exception(client,container,monkeypatch):
    from orchestrator.docker_services import DockerServiceError
    container.applications.create_app(UserAppCreate(appid="REVIEW-ASYNC",name="review",compose={"services":{"web":{"image":"alpine"}}},repository_url="https://example.invalid/repo.git",require_route="/health"))
    prepare_static_pass(container,"REVIEW-ASYNC")
    b=container.builds.queue_build("REVIEW-ASYNC", BuildCreate())
    rt=WorkerRuntime(container.settings,repository=container.repository,build_service=container.builds,catalog=container.catalog,jenkins=FakeJenkins([],build=None),gitlab=FakeGitLab("a"*40),docker_services=FakeDockerServices(None),poll_scheduler=lambda *a:None,route_checks=container.route_checks,dynamic_checks=None)
    class BadAdapter:
        def validate_build_compose(self,*a,**k):
            raise DockerServiceError("adapter failure mongodb://review:FAKE_ASYNC_SENTINEL@external.invalid/db")
    monkeypatch.setattr(rt,"_docker_validation_adapter",lambda:BadAdapter())
    rt.start_build(b.build_id,"review-task")
    response=client.get("/api/v1/jenkins/build_info/"+b.build_id)
    print("ASYNC_PUBLIC_RESULT",json.dumps({k:response.json().get(k) for k in ("error","error_detail","failure_logs","raw_log_excerpt")},ensure_ascii=False))
    assert "FAKE_ASYNC_SENTINEL" not in response.text
