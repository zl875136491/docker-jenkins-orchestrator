import json
import yaml
from pathlib import Path
from test_error_contract_integration import client, container
from orchestrator.docker_services import DockerSwarmAdapter
from orchestrator.jenkins import JenkinsQueueItem
from orchestrator.models import BuildCreate, UserAppCreate
from orchestrator.worker_runtime import WorkerRuntime
from test_worker_runtime import FakeDockerServices, FakeGitLab, FakeJenkins
from tests.route_check_fixtures import prepare_static_pass


def _scm_compose():
    # Use uploaded SCM compose
    compose_path = Path("/home/ubuntu/.cursor/projects/agent-repos-docker-jenkins-orchestrator/uploads/compose_f52e.yaml")
    if not compose_path.exists():
        compose_path = Path(__file__).parent.parent / "uploads" / "compose_f52e.yaml"
    return yaml.safe_load(compose_path.read_text())


def test_real_scm_compose_end_to_end_api_and_worker_no_crash(client, container):
    compose = _scm_compose()
    env = {
        "MONGO_URI": "mongodb://mongo:27017/scm_test",
        "MEILI_MASTER_KEY": "fake-meili-key-123456",
        "BUSINESS_MEILI_MASTER_KEY": "fake-biz-meili-key-123456",
        "SESSION_SECRET": "fake-session-secret-123456",
        "BOOTSTRAP_ADMIN_PASSWORD_HASH": "fake-hash-123456",
        "EXT_APP_FERNET_KEY": "fake-fernet-key-123456",
    }
    container.applications.create_app(
        UserAppCreate(
            appid="SCM-REAL-TEST",
            name="scm-real-test",
            compose=compose,
            environment=env,
            repository_url="https://example.invalid/scm.git",
            require_route="/health",
        )
    )
    prepare_static_pass(container, "SCM-REAL-TEST")

    # 1. Queue via real HTTP API (must be 202, not 422)
    r = client.post("/api/v1/jenkins/build_create/SCM-REAL-TEST", json={})
    assert r.status_code == 202, r.text
    build_id = r.json()["build_id"]

    # Non-terminal queued state must not fabricate error
    info_queued = client.get(f"/api/v1/jenkins/build_info/{build_id}").json()
    assert info_queued["status"] == "queued"
    assert info_queued.get("error") is None
    assert info_queued.get("error_detail") is None

    # 2. Run real WorkerRuntime.start_build with FakeJenkins/FakeGitLab/FakeDockerServices
    jenkins = FakeJenkins([JenkinsQueueItem(queue_url="https://jenkins.invalid/queue/item/1/", queue_id=1, build_number=101)])
    rt = WorkerRuntime(
        container.settings,
        repository=container.repository,
        build_service=container.builds,
        catalog=container.catalog,
        jenkins=jenkins,
        gitlab=FakeGitLab("a" * 40),
        docker_services=FakeDockerServices(None),
        poll_scheduler=lambda *a: None,
        route_checks=container.route_checks,
        dynamic_checks=None,
    )
    result = rt.start_build(build_id, "task-scm-1")
    info_after = client.get(f"/api/v1/jenkins/build_info/{build_id}").json()
    print("REAL_SCM_WORKER_RESULT", result)
    print("REAL_SCM_BUILD_INFO", json.dumps({k: info_after.get(k) for k in ("status", "stage", "error", "error_detail", "validation_warnings", "jenkins_build_number")}, ensure_ascii=False))

    assert result["status"] == "building", f"Worker failed instead of triggering Jenkins: {info_after}"
    assert info_after["status"] == "building"
    assert info_after.get("error") is None
    assert info_after.get("error_detail") is None

    warnings = info_after.get("validation_warnings")
    assert isinstance(warnings, list) and len(warnings) > 0, f"Missing validation_warnings: {warnings}"
    serialized = json.dumps(warnings, ensure_ascii=False)
    for forbidden in ["10.2.106.2", "10.2.101.32", "10.0.100.218", "fake-meili-key", "fake-session-secret", "mongodb://"]:
        assert forbidden not in serialized, f"Leaked value/default/URI fragment {forbidden} in warnings"


def test_real_scm_compose_warning_coverage_for_api_and_worker_and_hana_sql():
    adapter = DockerSwarmAdapter()
    compose = _scm_compose()
    env = {
        "MONGO_URI": "mongodb://mongo:27017/scm_test",
        "MEILI_MASTER_KEY": "fake-meili-key-123456",
        "BUSINESS_MEILI_MASTER_KEY": "fake-biz-meili-key-123456",
        "SESSION_SECRET": "fake-session-secret-123456",
        "BOOTSTRAP_ADMIN_PASSWORD_HASH": "fake-hash-123456",
        "EXT_APP_FERNET_KEY": "fake-fernet-key-123456",
    }
    resolved = WorkerRuntime._resolve_application_environment(compose, env)
    warnings = adapter.validate_build_compose(resolved, environment=env)
    print("ADAPTER_WARNINGS_RESOLVED", json.dumps(warnings, ensure_ascii=False))
    ecn_entries = [w for w in warnings if w.get("environment_key") == "ECN_DB_HOST" or w.get("name") == "ECN_DB_HOST"]
    assert len(ecn_entries) >= 1
    services_seen = set()
    for w in ecn_entries:
        if w.get("service"):
            services_seen.add(w["service"])
        for loc in w.get("locations", []):
            if loc.get("service"):
                services_seen.add(loc["service"])
    assert {"api", "worker"}.issubset(services_seen), f"Missing api/worker coverage: {ecn_entries}"
    all_keys = {w.get("environment_key") or w.get("name") for w in warnings}
    print("DETECTED_WARNING_KEYS", sorted(k for k in all_keys if k))
    assert "HANA_HOST" in all_keys and "SQL_HOST" in all_keys, f"HANA_HOST/SQL_HOST missed by detector: {sorted(k for k in all_keys if k)}"


def test_broken_uri_still_rejected_with_invalid_stateful_endpoint(client, container):
    compose = {
        "services": {
            "api": {
                "image": "api:latest",
                "environment": {
                    "DATABASE_URL": "postgres://user:FakePass99@db.example:badport/appdb",
                },
            }
        }
    }
    container.applications.create_app(
        UserAppCreate(
            appid="BROKEN-URI",
            name="broken-uri",
            compose=compose,
            repository_url="https://example.invalid/repo.git",
            require_route="/health",
        )
    )
    prepare_static_pass(container, "BROKEN-URI")
    r = client.post("/api/v1/jenkins/build_create/BROKEN-URI", json={})
    assert r.status_code == 202
    build_id = r.json()["build_id"]
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
    res = rt.start_build(build_id, "task-broken")
    data = client.get(f"/api/v1/jenkins/build_info/{build_id}").json()
    print("BROKEN_URI_RESULT", res, json.dumps({k: data.get(k) for k in ("status", "error", "error_detail", "validation_warnings")}, ensure_ascii=False))
    assert res["status"] == "failed", f"Malformed URI was not rejected: {data}"
    assert (data.get("error_detail") or {}).get("subcode") == "INVALID_STATEFUL_ENDPOINT"
    assert "FakePass99" not in json.dumps(data)


def test_external_docker_resources_still_rejected_and_422_missing_preserved(client, container):
    compose_422 = {
        "services": {
            "api": {
                "image": "api:latest",
                "environment": {
                    "REQUIRED_SECRET": "${REQUIRED_SECRET:?need secret}",
                    "ECN_DB_HOST": "${ECN_DB_HOST:-10.2.106.2}",
                },
            },
            "worker": {
                "image": "worker:latest",
                "environment": {
                    "REQUIRED_SECRET": "${REQUIRED_SECRET:?need secret}",
                    "ECN_DB_HOST": "${ECN_DB_HOST:-10.2.106.2}",
                },
            },
        }
    }
    container.applications.create_app(
        UserAppCreate(
            appid="MISSING-AND-EXT",
            name="missing-and-ext",
            compose=compose_422,
            repository_url="https://example.invalid/repo.git",
            require_route="/health",
        )
    )
    prepare_static_pass(container, "MISSING-AND-EXT")
    r = client.post("/api/v1/jenkins/build_create/MISSING-AND-EXT", json={})
    assert r.status_code == 422
    ed = r.json()["error_detail"]
    assert ed["code"] == "COMPOSE_CONFIG_ERROR" and ed["subcode"] == "MISSING_ENVIRONMENT_VARIABLES"
    assert len(ed["missing_variables"]) == 1
    assert ed["missing_variables"][0]["name"] == "REQUIRED_SECRET"
    assert {loc["service"] for loc in ed["missing_variables"][0]["locations"]} == {"api", "worker"}
    assert "need secret" not in r.text and "10.2.106.2" not in r.text
    assert container.repository.list_builds(appid="MISSING-AND-EXT")[1] == 0

    compose_vol = {
        "services": {"api": {"image": "api:latest", "volumes": ["/etc/passwd:/tmp/p:ro"]}},
    }
    container.applications.create_app(
        UserAppCreate(
            appid="EXT-VOL",
            name="ext-vol",
            compose=compose_vol,
            repository_url="https://example.invalid/repo.git",
            require_route="/health",
        )
    )
    prepare_static_pass(container, "EXT-VOL")
    b = container.builds.queue_build("EXT-VOL", BuildCreate())
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
    res = rt.start_build(b.build_id, "task-vol")
    assert res["status"] == "failed"
