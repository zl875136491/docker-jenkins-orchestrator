"""测试事件和alert与 build_info 的失败契约一致性。"""
from test_error_contract_integration import client, container
from orchestrator.models import UserAppCreate, BuildCreate
from orchestrator.worker_runtime import WorkerRuntime
from orchestrator.docker_services import DockerServiceError
from test_worker_runtime import FakeJenkins, FakeDockerServices, FakeGitLab
from tests.route_check_fixtures import prepare_static_pass


def test_worker_exception_does_not_leak_to_events_or_alerts(client, container, monkeypatch):
    """worker 异常后：build_info 回传 raw_log；alert.message 与 error 一致。"""
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

    class BadAdapter:
        def validate_build_compose(self, *a, **k):
            exc = DockerServiceError("opaque")
            exc.raw_log_excerpt = "mongodb://review:SecretMarkerX9@external.invalid/db"
            exc.failure_logs = {"source": "validating", "summary": "连接失败 LogMarkerY3"}
            raise exc

    monkeypatch.setattr(rt, "_docker_validation_adapter", lambda: BadAdapter())
    rt.start_build(b.build_id, "review-task")

    r_info = client.get(f"/api/v1/jenkins/build_info/{b.build_id}")
    assert r_info.status_code == 200
    info_data = r_info.json()
    assert info_data["raw_log_excerpt"]
    assert "SecretMarkerX9" in info_data["raw_log_excerpt"]
    assert info_data["error"]
    assert info_data["failure_logs"]["summary"] == info_data["error"]

    r_events = client.get(f"/api/v1/jenkins/app_events/{appid}")
    assert r_events.status_code == 200
    events_data = r_events.json()

    if isinstance(events_data, list):
        alerts = [e for e in events_data if e.get("kind") == "build.alert"]
    else:
        alerts = [e for e in events_data.get("events", []) if e.get("kind") == "build.alert"]

    assert len(alerts) > 0, "No build.alert events found"
    alert = alerts[0]
    assert alert["message"] == info_data["error"]
