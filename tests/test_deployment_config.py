from pathlib import Path

import yaml

from orchestrator.config import Settings


COMPOSE_PATH = Path("docker-compose.yml")


def test_compose_runs_api_worker_beat_and_durable_dependencies() -> None:
    document = yaml.safe_load(COMPOSE_PATH.read_text())
    services = document["services"]

    assert {"api", "worker", "beat", "mongo", "redis"}.issubset(services)
    assert {"mongo-data", "redis-data", "beat-data"}.issubset(document["volumes"])
    assert services["api"]["depends_on"]["mongo"]["condition"] == "service_healthy"
    assert services["worker"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["beat"]["depends_on"]["mongo"]["condition"] == "service_healthy"
    assert services["mongo"]["healthcheck"]
    assert services["redis"]["healthcheck"]
    assert services["worker"]["volumes"][0]["target"] == "/var/run/docker.sock"


def test_compose_runtime_environment_enforces_mongo_celery_and_separate_queues() -> None:
    document = yaml.safe_load(COMPOSE_PATH.read_text())
    environment = document["x-orchestrator-runtime-environment"]
    worker_environment = document["x-orchestrator-worker-environment"]

    assert environment["ORCHESTRATOR_STORAGE_BACKEND"] == "mongo"
    assert environment["ORCHESTRATOR_TASK_DISPATCHER"] == "celery"
    assert "redis://redis:6379/0" in environment["ORCHESTRATOR_CELERY_BROKER_URL"]
    assert "ORCHESTRATOR_CELERY_BUILD_QUEUE" in environment
    assert "ORCHESTRATOR_CELERY_IMAGES_QUEUE" in environment
    assert "ORCHESTRATOR_JENKINS_USER" in worker_environment
    assert "ORCHESTRATOR_HARBOR_PASSWORD" in worker_environment


def test_production_settings_accept_the_compose_service_endpoints() -> None:
    settings = Settings(
        environment="production",
        storage_backend="mongo",
        task_dispatcher="celery",
        mongodb_url="mongodb://mongo:27017/orchestrator",
        celery_broker_url="redis://redis:6379/0",
        data_encryption_key="zJorGbC7YvDCKxmdx2r1Su03Ii1_lDbY3dOwqmyyy1E=",
        jenkins_url="https://jenkins.example",
        harbor_url="https://harbor.example",
        docker_base_url="unix:///var/run/docker.sock",
    )

    assert settings.storage_backend == "mongo"
    assert settings.task_dispatcher == "celery"
