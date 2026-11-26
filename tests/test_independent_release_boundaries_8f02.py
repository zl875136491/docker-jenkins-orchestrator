import json
from orchestrator.services import BuildInputError
from orchestrator.models import UserAppCreate,BuildCreate
from orchestrator.worker_runtime import WorkerRuntime
from orchestrator.docker_services import DockerServiceError
from test_worker_runtime import FakeJenkins,FakeDockerServices,FakeGitLab
from tests.route_check_fixtures import prepare_static_pass
from test_error_contract_integration import client,container

def setup_app(container,appid,compose):
    container.applications.create_app(UserAppCreate(appid=appid,name="review",compose=compose,repository_url="https://example.invalid/repo.git",require_route="/health"))
    prepare_static_pass(container,appid)

def test_real_25_missing_variables_are_not_truncated(client,container):
    setup_app(container,"REVIEW-25",{"services":{"web":{"image":"alpine","environment":[f"K{i}=${{NEED_{i}}}" for i in range(25)]}}})
    r=client.post("/api/v1/jenkins/build_create/REVIEW-25",json={})
    actual=r.json()["error_detail"]["missing_variables"]
    print("AGGREGATE_COUNT",len(actual))
    assert r.status_code==422 and len(actual)==25

def test_real_11_locations_are_not_truncated(client,container):
    setup_app(container,"REVIEW-11",{"services":{f"web{i}":{"image":"alpine","environment":["K=${SHARED}"]} for i in range(11)}})
    r=client.post("/api/v1/jenkins/build_create/REVIEW-11",json={})
    actual=r.json()["error_detail"]["missing_variables"][0]["locations"]
    print("LOCATIONS_COUNT",len(actual))
    assert len(actual)==11

def test_unknown_classification_action_cannot_be_exception_text(client,container,monkeypatch):
    marker="ActionMarkerV8q2"
    def fail(*a,**k):raise BuildInputError("opaque",error_detail={"category":"configuration_error","responsible_party":"application_developer","user_action_required":True,"action_guidance":"诊断结果 "+marker})
    monkeypatch.setattr(container.builds,"queue_build",fail)
    r=client.post("/api/v1/jenkins/build_create/REVIEW",json={})
    print("ACTION_METADATA_RESULT",r.text)
    assert marker not in r.text
    assert r.json()["error_detail"]["category"]=="unknown_error"

def test_unknown_worker_logs_and_flags_are_safe(client,container,monkeypatch):
    setup_app(container,"REVIEW-LOGS",{"services":{"web":{"image":"alpine"}}})
    b=container.builds.queue_build("REVIEW-LOGS",BuildCreate())
    rt=WorkerRuntime(container.settings,repository=container.repository,build_service=container.builds,catalog=container.catalog,jenkins=FakeJenkins([],build=None),gitlab=FakeGitLab("a"*40),docker_services=FakeDockerServices(None),poll_scheduler=lambda *a:None,route_checks=container.route_checks,dynamic_checks=None)
    class BadAdapter:
        def validate_build_compose(self,*a,**k):
            exc=DockerServiceError("opaque")
            exc.raw_log_excerpt="mongodb://review:RawMarkerA7v2@external.invalid/db"
            exc.failure_logs={"source":"validating","summary":"连接失败 LogMarkerB8v3"}
            raise exc
    monkeypatch.setattr(rt,"_docker_validation_adapter",lambda:BadAdapter())
    rt.start_build(b.build_id,"review-task")
    r=client.get("/api/v1/jenkins/build_info/"+b.build_id)
    data=r.json()
    print("PUBLIC_LOG_FIELDS",json.dumps({k:data.get(k) for k in ("error","error_detail","failure_logs","raw_log_excerpt")},ensure_ascii=False))
    # 用户授权回传 raw_log；结构化 error 仍用安全 summary
    assert data["raw_log_excerpt"] and "RawMarkerA7v2" in data["raw_log_excerpt"]
    assert "LogMarkerB8v3" not in (data.get("error") or "")
    assert data["error_detail"]["recognized"] is False
    assert data["error_detail"]["user_action_required"] is False
