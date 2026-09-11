from pathlib import Path

import yaml


def test_compose_has_api_worker_and_stateful_dependencies() -> None:
    document = yaml.safe_load(Path("docker-compose.yml").read_text())
    assert {"api", "worker", "mongo", "redis"}.issubset(document["services"])
    assert "mongo-data" in document["volumes"]
