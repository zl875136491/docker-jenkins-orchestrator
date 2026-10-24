from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orchestrator.celery_app import create_celery_app
from orchestrator.celery_dispatcher import CeleryTaskDispatcher
from orchestrator.config import Settings
from orchestrator.tasks import InMemoryTaskDispatcher


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["docker-compose.yml", "docker-compose.test.yml"])
def test_worker_has_local_codex_authentication_and_persistent_login_without_api_leak(name):
    document = yaml.safe_load((ROOT / name).read_text())
    worker = document["services"]["worker"]
    assert worker["environment"]["OPENAI_API_KEY"] == "${OPENAI_API_KEY:-}"
    assert "CODEX_API_KEY" not in worker["environment"]
    assert "codex-data:/root/.codex" in worker["volumes"]
    assert "codex-data" in document["volumes"]
    for service in ("api", "beat"):
        assert "OPENAI_API_KEY" not in document["services"][service]["environment"]
        assert all("codex-data" not in str(volume) for volume in document["services"][service].get("volumes", []))
    for service in ("api", "worker", "beat"):
        environment = document["services"][service]["environment"]
        assert environment["ORCHESTRATOR_CODEX_EXECUTABLE"] == "${ORCHESTRATOR_CODEX_EXECUTABLE:-codex}"
        assert environment["ORCHESTRATOR_ROUTE_CHECK_LEASE_SECONDS"] == "${ORCHESTRATOR_ROUTE_CHECK_LEASE_SECONDS:-120}"
        assert environment["ORCHESTRATOR_ROUTE_PROBE_TIMEOUT_SECONDS"] == "${ORCHESTRATOR_ROUTE_PROBE_TIMEOUT_SECONDS:-15}"
        assert environment["ORCHESTRATOR_ROUTE_PROBE_MAX_RESPONSE_BYTES"] == "${ORCHESTRATOR_ROUTE_PROBE_MAX_RESPONSE_BYTES:-65536}"
        if name == "docker-compose.yml":
            for key in (
                "CODEX_PROVIDER_NAME", "CODEX_PROVIDER_ENDPOINT", "CODEX_DEFAULT_MODEL", "CODEX_DEFAULT_EFFORT",
                "CODEX_PROVIDER_ID", "CODEX_PROVIDER_WIRE_API", "CODEX_PROVIDER_REQUIRES_OPENAI_AUTH",
            ):
                assert key in environment
    if name == "docker-compose.yml":
        assert document["services"]["api"]["environment"]["CODEX_API_KEY"] == "${CODEX_API_KEY:-}"
        assert document["services"]["worker"]["depends_on"]["api"]["condition"] == "service_healthy"


def test_codex_https_proxy_is_optional_and_forwarded_only_to_production_worker():
    document = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    worker = document["services"]["worker"]["environment"]
    assert worker["ORCHESTRATOR_CODEX_HTTPS_PROXY"] == "${ORCHESTRATOR_CODEX_HTTPS_PROXY:-}"
    for service in ("api", "beat"):
        assert "HTTPS_PROXY" not in document["services"][service]["environment"]
        assert "ORCHESTRATOR_CODEX_HTTPS_PROXY" not in document["services"][service]["environment"]
    assert "HTTPS_PROXY" not in worker
    example = (ROOT / ".env.example").read_text()
    assert "ORCHESTRATOR_CODEX_HTTPS_PROXY=\n" in example


def test_codex_proxy_setting_uses_a_dedicated_environment_variable(monkeypatch):
    from orchestrator.config import Settings

    monkeypatch.setenv("ORCHESTRATOR_CODEX_HTTPS_PROXY", "http://codex-proxy.example:7890")
    configured = Settings(_env_file=None)

    assert configured.codex_https_proxy.get_secret_value() == "http://codex-proxy.example:7890"


def test_dockerfile_installs_fixed_codex_cli_and_verifies_the_copied_runtime():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "FROM node:22-bookworm-slim AS codex-cli" in dockerfile
    assert "ARG CODEX_CLI_VERSION=0.147.0" in dockerfile
    assert '"@openai/codex@${CODEX_CLI_VERSION}"' in dockerfile
    assert "COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert "COPY --from=codex-cli /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai" in dockerfile
    assert "&& codex --version" in dockerfile
    assert "&& codex exec --help >/dev/null" in dockerfile
    assert "OPENAI_API_KEY" not in dockerfile
    assert "COPY . ." not in dockerfile
    for asset in ("orchestrator", "templates", "docs", "webui"):
        assert f"COPY {asset}/ {asset}/" in dockerfile
    assert "COPY main.py ." in dockerfile


@pytest.mark.parametrize("name", ["docker-compose.yml", "docker-compose.test.yml"])
def test_debian_mirror_is_configurable_only_for_application_image_builds(name):
    document = yaml.safe_load((ROOT / name).read_text())
    build = document["services"]["api"]["build"]
    assert build == {"context": ".", "args": {
        "DEBIAN_MIRROR": "${ORCHESTRATOR_DEBIAN_MIRROR:-https://deb.debian.org}",
    }}
    for service in ("api", "worker", "beat"):
        definition = document["services"][service]
        assert definition["build"] == build
        assert "ORCHESTRATOR_DEBIAN_MIRROR" not in definition["environment"]
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "ARG DEBIAN_MIRROR=https://deb.debian.org" in dockerfile
    assert "Acquire::https::Timeout=30" in dockerfile
    assert "--allow-unauthenticated" not in dockerfile


def test_route_settings_and_env_example_match():
    settings = Settings()
    assert settings.codex_executable == "codex"
    assert settings.route_check_lease_seconds == 120
    assert settings.route_probe_timeout_seconds == 15
    assert settings.route_probe_max_response_bytes == 65536
    example = (ROOT / ".env.example").read_text()
    assert "OPENAI_API_KEY=\n" in example
    for name, value in (("CODEX_EXECUTABLE", "codex"), ("ROUTE_CHECK_LEASE_SECONDS", "120"),
                        ("ROUTE_PROBE_TIMEOUT_SECONDS", "15"), ("ROUTE_PROBE_MAX_RESPONSE_BYTES", "65536")):
        assert f"ORCHESTRATOR_{name}={value}\n" in example


@pytest.mark.parametrize("fields", [
    {"codex_executable": ""}, {"route_check_lease_seconds": 9},
    {"route_probe_timeout_seconds": 0}, {"route_probe_timeout_seconds": 121},
    {"route_probe_max_response_bytes": 1023}, {"route_probe_max_response_bytes": 1048577},
])
def test_invalid_route_runtime_settings_are_rejected(fields):
    with pytest.raises(ValueError):
        Settings(**fields)


def test_static_and_dynamic_dispatch_use_identified_generations_and_build_runs():
    settings = Settings(celery_build_queue="route-builds")
    calls = []
    broker = SimpleNamespace(send_task=lambda name, *, args, queue: (
        calls.append((name, args, queue)) or SimpleNamespace(id="task-id")))
    dispatcher = CeleryTaskDispatcher(settings, celery_app=broker)
    assert dispatcher.dispatch_static_check("check", 4).task_id == "task-id"
    assert dispatcher.dispatch_dynamic_check("build", "run").task_id == "task-id"
    assert calls == [("orchestrator.pipeline.static_route_check", ["check", 4], "route-builds"),
                     ("orchestrator.pipeline.dynamic_route_check", ["build", "run"], "route-builds")]
    memory = InMemoryTaskDispatcher()
    memory.dispatch_static_check("check", 4)
    memory.dispatch_dynamic_check("build", "run")
    assert memory.static_check_generations == [("check", 4)]
    assert memory.dynamic_check_runs == [("build", "run")]


def test_beat_routes_recoveries_to_build_queue_with_no_result_backend():
    settings = Settings(celery_build_queue="route-builds", celery_recovery_interval_seconds=42)
    app = create_celery_app(settings)
    try:
        recovery = app.conf.beat_schedule["recover-route-checks"]
        assert recovery["task"] == "orchestrator.pipeline.recover_route_checks"
        assert recovery["schedule"].total_seconds() == 42
        assert recovery["options"] == {"queue": "route-builds"}
        assert app.conf.result_backend is None
        assert app.conf.task_acks_late is True
        assert app.conf.task_reject_on_worker_lost is True
    finally:
        app.close()
