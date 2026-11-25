import json
from orchestrator.services import BuildInputError
from orchestrator.models import UserAppCreate
from tests.route_check_fixtures import prepare_static_pass
from test_error_contract_integration import client, container

def test_unknown_short_chinese_is_not_safe(client,container,monkeypatch):
    marker="Zq8mN47wA12b"
    def fail(*a,**k):raise BuildInputError("连接失败 "+marker)
    monkeypatch.setattr(container.builds,"queue_build",fail)
    r=client.post("/api/v1/jenkins/build_create/REVIEW",json={})
    print("CHINESE_PROBE",r.text)
    assert marker not in r.text

def test_missing_variables_preserve_nested_contract(client,container):
    compose={"services":{"web":{"image":"alpine","environment":{"TOKEN":"${SHARED:?fake hint}","PLAIN":"${OTHER}"}},"worker":{"image":"alpine","environment":["TOKEN=${SHARED:?fake hint}"]}}}
    container.applications.create_app(UserAppCreate(appid="REVIEW-NESTED",name="review",compose=compose,repository_url="https://example.invalid/repo.git",require_route="/health",environment={"SHARED":""}))
    prepare_static_pass(container,"REVIEW-NESTED")
    before_dispatch=len(container.dispatcher.dispatched)
    r=client.post("/api/v1/jenkins/build_create/REVIEW-NESTED",json={})
    print("NESTED_PROBE",r.text)
    assert r.status_code==422
    missing=r.json()["error_detail"]["missing_variables"]
    by={v["name"]:v for v in missing}
    shared=by["SHARED"]
    assert shared["reason"]=="empty" and shared["operator"]==":?"
    assert shared["locations"]==[{"service":"web","environment_key":"TOKEN","config_path":"services.web.environment"},{"service":"worker","environment_key":"TOKEN","config_path":"services.worker.environment"}]
    assert by["OTHER"]["reason"]=="missing" and by["OTHER"]["operator"] is None
    assert by["OTHER"]["locations"]==[{"service":"web","environment_key":"PLAIN","config_path":"services.web.environment"}]
    assert "fake hint" not in r.text
    assert len(container.repository.builds)==0 and len(container.dispatcher.dispatched)==before_dispatch

def test_unknown_existing_detail_cannot_bypass_safety(client,container,monkeypatch):
    marker="UnknownMetaR8k2V"
    def fail(*a,**k):raise BuildInputError("opaque",error_detail={"category":"unknown_error","responsible_party":"unknown","summary":"连接失败 "+marker})
    monkeypatch.setattr(container.builds,"queue_build",fail)
    r=client.post("/api/v1/jenkins/build_create/REVIEW",json={})
    print("EXISTING_CLASS_PROBE",r.text)
    assert marker not in r.text

def test_missing_metadata_not_merely_whitelisted_by_key(client,container,monkeypatch):
    marker="MetadataValueQ8v2"
    def fail(*a,**k):raise BuildInputError("opaque",error_detail={"code":"COMPOSE_CONFIG_ERROR","subcode":"MISSING_ENVIRONMENT_VARIABLES","missing_variables":[{"name":"SAFE_NAME","reason":"missing","operator":None,"locations":[{"service":"web","environment_key":"KEY","config_path":"services.web.environment"}],"value":marker}]})
    monkeypatch.setattr(container.builds,"queue_build",fail)
    r=client.post("/api/v1/jenkins/build_create/REVIEW",json={})
    print("METADATA_VALUE_PROBE",r.text)
    assert marker not in r.text

def test_async_unknown_detail_cannot_override_safe_error(client,container,monkeypatch):
    from orchestrator.docker_services import DockerServiceError
    from orchestrator.worker_runtime import WorkerRuntime
    from orchestrator.models import BuildCreate
    from test_worker_runtime import FakeJenkins,FakeGitLab,FakeDockerServices
    container.applications.create_app(UserAppCreate(appid="REVIEW-ASYNC",name="review",compose={"services":{"web":{"image":"alpine"}}},repository_url="https://example.invalid/repo.git",require_route="/health"))
    prepare_static_pass(container,"REVIEW-ASYNC")
    b=container.builds.queue_build("REVIEW-ASYNC", BuildCreate())
    rt=WorkerRuntime(container.settings,repository=container.repository,build_service=container.builds,catalog=container.catalog,jenkins=FakeJenkins([],build=None),gitlab=FakeGitLab("a"*40),docker_services=FakeDockerServices(None),poll_scheduler=lambda *a:None,route_checks=container.route_checks,dynamic_checks=None)
    class BadAdapter:
        def validate_build_compose(self,*a,**k):
            exc=DockerServiceError("opaque")
            exc.error_detail={"summary":"诊断结果 AsyncMetaR9v8", "source":"validating"}
            raise exc
    monkeypatch.setattr(rt,"_docker_validation_adapter",lambda:BadAdapter())
    rt.start_build(b.build_id,"review-task")
    response=client.get("/api/v1/jenkins/build_info/"+b.build_id)
    print("ASYNC_PUBLIC_RESULT",json.dumps({k:response.json().get(k) for k in ("error","error_detail","failure_logs","raw_log_excerpt")},ensure_ascii=False))
    assert "AsyncMetaR9v8" not in response.text
