"""测试事件和alert不泄漏raw日志"""
from test_error_contract_integration import client, container
from orchestrator.models import UserAppCreate, BuildCreate
from orchestrator.worker_runtime import WorkerRuntime
from orchestrator.docker_services import DockerServiceError
from test_worker_runtime import FakeJenkins, FakeDockerServices, FakeGitLab
from tests.route_check_fixtures import prepare_static_pass


def test_worker_exception_does_not_leak_to_events_or_alerts(client, container, monkeypatch):
    """worker异常后build_info和app_events都不泄漏secret，alert.message与error一致"""
    appid = "REVIEW-EVENT"
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
    
    b = container.builds.queue_build(appid, BuildCreate())
    
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
        dynamic_checks=None,
    )
    
    # 注入含secret的异常
    class BadAdapter:
        def validate_build_compose(self, *a, **k):
            exc = DockerServiceError("opaque")
            exc.raw_log_excerpt = "mongodb://review:SecretMarkerX9@external.invalid/db"
            exc.failure_logs = {"source": "validating", "summary": "连接失败 LogMarkerY3"}
            raise exc
    
    monkeypatch.setattr(rt, "_docker_validation_adapter", lambda: BadAdapter())
    rt.start_build(b.build_id, "review-task")
    
    # 1. build_info不泄漏
    r_info = client.get(f"/api/v1/jenkins/build_info/{b.build_id}")
    assert r_info.status_code == 200
    info_data = r_info.json()
    assert "SecretMarkerX9" not in r_info.text
    assert "LogMarkerY3" not in r_info.text
    assert info_data["raw_log_excerpt"] is None
    assert info_data["failure_logs"]["summary"] == info_data["error"]
    
    # 2. app_events不泄漏，且alert.message与build.error一致
    r_events = client.get(f"/api/v1/jenkins/app_events/{appid}")
    assert r_events.status_code == 200
    events_data = r_events.json()
    assert "SecretMarkerX9" not in r_events.text
    assert "LogMarkerY3" not in r_events.text
    
    # 找到build.alert事件（kind字段）
    if isinstance(events_data, list):
        alerts = [e for e in events_data if e.get("kind") == "build.alert"]
    else:
        # 可能是{"events": [...]}格式
        alerts = [e for e in events_data.get("events", []) if e.get("kind") == "build.alert"]
    
    assert len(alerts) > 0, f"No build.alert events found"
    alert = alerts[0]
    # alert.message与build.error一致
    assert alert["message"] == info_data["error"]
    # details不含raw_log_excerpt_preview
    assert "raw_log_excerpt_preview" not in alert.get("details", {})
