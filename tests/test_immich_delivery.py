from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from orchestrator.docker_services import DockerServiceError, DockerSwarmAdapter


_SERVER_IMAGE = "ghcr.io/immich-app/immich-server:release@sha256:" + "a" * 64
_ML_IMAGE = "ghcr.io/immich-app/immich-machine-learning:release@sha256:" + "b" * 64
_REDIS_IMAGE = "docker.io/valkey/valkey:8-bookworm@sha256:" + "c" * 64
_DATABASE_IMAGE = "ghcr.io/immich-app/postgres:14-vectorchord@sha256:" + "d" * 64


IMMICH_COMPOSE: dict[str, Any] = {
    "version": "3.8",
    "services": {
        "immich-server": {
            "image": _SERVER_IMAGE,
            "volumes": [
                {"type": "bind", "source": "/srv/immich/upload", "target": "/data"},
                {"type": "bind", "source": "/etc/localtime", "target": "/etc/localtime", "read_only": True},
            ],
            "env_file": [".env"],
            "ports": ["2283:2283"],
            "depends_on": {
                "redis": {"condition": "service_healthy"},
                "database": {"condition": "service_healthy"},
            },
            "restart": "always",
            "healthcheck": {
                "test": ["CMD", "curl", "-f", "http://localhost:2283/api/server/ping"],
                "interval": "30s",
                "timeout": "10s",
                "retries": 3,
            },
        },
        "immich-machine-learning": {
            "image": _ML_IMAGE,
            "volumes": ["model-cache:/cache"],
            "env_file": [".env"],
            "restart": "always",
            "healthcheck": {"test": ["CMD", "curl", "-f", "http://localhost:3003/"], "interval": "30s"},
        },
        "redis": {
            "image": _REDIS_IMAGE,
            "restart": "always",
            "healthcheck": {"test": ["CMD", "redis-cli", "ping"], "interval": "10s", "timeout": "5s"},
        },
        "database": {
            "image": _DATABASE_IMAGE,
            "environment": {
                "POSTGRES_DB": "immich",
                "POSTGRES_PASSWORD": "postgres",
                "POSTGRES_USER": "postgres",
            },
            "volumes": [
                {"type": "bind", "source": "/srv/immich/postgres", "target": "/var/lib/postgresql/data"}
            ],
            "shm_size": "128mb",
            "restart": "always",
            "healthcheck": {"test": ["CMD", "pg_isready"], "interval": "10s"},
        },
    },
}


class NotFound(Exception):
    status_code = 404


class FakeNetwork:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = f"network-{name}-id"


class FakeNetworks:
    def __init__(self) -> None:
        self.items: dict[str, FakeNetwork] = {}
        self.get_calls: list[str] = []
        self.create_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, name: str) -> FakeNetwork:
        self.get_calls.append(name)
        if name not in self.items:
            raise NotFound(name)
        return self.items[name]

    def create(self, name: str, **kwargs: Any) -> FakeNetwork:
        self.create_calls.append((name, kwargs))
        network = FakeNetwork(name)
        self.items[name] = network
        return network


class FakeService:
    def __init__(self, name: str) -> None:
        self.id = f"service-{name}-id"


class FakeServices:
    def __init__(self) -> None:
        self.items: dict[str, FakeService] = {}
        self.get_calls: list[str] = []
        self.create_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, name: str) -> FakeService:
        self.get_calls.append(name)
        if name not in self.items:
            raise NotFound(name)
        return self.items[name]

    def create(self, image: str, **kwargs: Any) -> FakeService:
        self.create_calls.append((image, kwargs))
        service = FakeService(kwargs["name"])
        self.items[kwargs["name"]] = service
        return service


class FakeDocker:
    def __init__(self) -> None:
        self.networks = FakeNetworks()
        self.services = FakeServices()


def _created_services(docker: FakeDocker) -> dict[str, tuple[str, dict[str, Any]]]:
    return {kwargs["name"]: (image, kwargs) for image, kwargs in docker.services.create_calls}


def test_immich_delivery_rejects_unresolved_env_file_before_side_effects() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError, match="env_file"):
        adapter.deploy("immich", IMMICH_COMPOSE)

    # An invalid artifact must not leave behind a network or a partially
    # created application.  Jenkins must resolve .env into environment first.
    assert docker.networks.create_calls == []
    assert docker.services.create_calls == []


@pytest.mark.parametrize("field", ["container_name", "secrets", "configs", "gpus", "networks"])
def test_swarm_adapter_rejects_unmapped_compose_features_before_side_effects(field: str) -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)
    compose = {"services": {"api": {"image": "example/api@sha256:" + "e" * 64, field: {}}}}

    with pytest.raises(DockerServiceError, match="unsupported fields"):
        adapter.deploy("demo", compose)

    assert docker.networks.create_calls == []
    assert docker.services.create_calls == []


def test_immich_delivery_maps_digest_images_bind_volumes_port_and_runtime_fields() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)
    compose = deepcopy(IMMICH_COMPOSE)
    for service_name in ("immich-server", "immich-machine-learning"):
        service = compose["services"][service_name]
        service.pop("env_file")
        service["environment"] = {
            "DB_HOSTNAME": "database",
            "REDIS_HOSTNAME": "redis",
        }

    deployment = adapter.deploy("immich", compose)

    assert deployment.appid == "immich"
    assert deployment.network_name == "orchestrator-immich"
    assert sorted(service.service_name for service in deployment.services) == [
        "immich-database",
        "immich-machine-learning",
        "immich-redis",
        "immich-server",
    ]
    created = _created_services(docker)
    assert set(created) == {
        "immich-server",
        "immich-machine-learning",
        "immich-redis",
        "immich-database",
    }

    server_image, server_kwargs = created["immich-server"]
    assert server_image == _SERVER_IMAGE
    assert "@sha256:" in server_image
    assert server_kwargs["mounts"] == [
        "/srv/immich/upload:/data:rw",
        "/etc/localtime:/etc/localtime:ro",
    ]
    assert server_kwargs["endpoint_spec"] == {
        "Mode": "vip",
        "Ports": [
            {
                "Protocol": "tcp",
                "TargetPort": 2283,
                "PublishMode": "ingress",
                "PublishedPort": 2283,
            }
        ],
    }
    assert server_kwargs["restart_policy"] == {"Condition": "any"}
    assert server_kwargs["healthcheck"] == {
        "Test": ["CMD", "curl", "-f", "http://localhost:2283/api/server/ping"],
        "Interval": 30_000_000_000,
        "Timeout": 10_000_000_000,
        "Retries": 3,
    }
    assert next(service.endpoint for service in deployment.services if service.service_name == "immich-server") == (
        "immich-server:2283"
    )

    machine_learning_image, machine_learning_kwargs = created["immich-machine-learning"]
    assert machine_learning_image == _ML_IMAGE
    assert machine_learning_kwargs["mounts"] == ["model-cache:/cache"]
    assert machine_learning_kwargs["restart_policy"] == {"Condition": "any"}
    assert machine_learning_kwargs["healthcheck"] == {
        "Test": ["CMD", "curl", "-f", "http://localhost:3003/"],
        "Interval": 30_000_000_000,
    }

    redis_image, redis_kwargs = created["immich-redis"]
    assert redis_image == _REDIS_IMAGE
    assert "@sha256:" in redis_image
    assert redis_kwargs["restart_policy"] == {"Condition": "any"}

    database_image, database_kwargs = created["immich-database"]
    assert database_image == _DATABASE_IMAGE
    assert "@sha256:" in database_image
    assert database_kwargs["env"] == [
        "POSTGRES_DB=immich",
        "POSTGRES_PASSWORD=postgres",
        "POSTGRES_USER=postgres",
    ]
    assert database_kwargs["mounts"] == [
        "/srv/immich/postgres:/var/lib/postgresql/data:rw",
        {
            "Type": "tmpfs",
            "Source": "",
            "Target": "/dev/shm",
            "ReadOnly": False,
            "TmpfsOptions": {"SizeBytes": 134_217_728},
        },
    ]
    assert database_kwargs["restart_policy"] == {"Condition": "any"}
