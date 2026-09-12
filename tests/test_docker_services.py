import pytest

from orchestrator.docker_services import DockerServiceError, DockerSwarmAdapter


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
        self.create_calls: list[tuple[str, dict]] = []

    def get(self, name: str) -> FakeNetwork:
        self.get_calls.append(name)
        if name not in self.items:
            raise NotFound(name)
        return self.items[name]

    def create(self, name: str, **kwargs) -> FakeNetwork:
        self.create_calls.append((name, kwargs))
        network = FakeNetwork(name)
        self.items[name] = network
        return network


class FakeService:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = f"service-{name}-id"
        self.attrs = {"ID": self.id}
        self.update_calls: list[dict] = []
        self.remove_calls = 0

    def update(self, **kwargs) -> None:
        self.update_calls.append(kwargs)

    def remove(self) -> None:
        self.remove_calls += 1


class FakeServices:
    def __init__(self) -> None:
        self.items: dict[str, FakeService] = {}
        self.get_calls: list[str] = []
        self.create_calls: list[tuple[str, dict]] = []

    def get(self, name: str) -> FakeService:
        self.get_calls.append(name)
        if name not in self.items:
            raise NotFound(name)
        return self.items[name]

    def create(self, image: str, **kwargs) -> FakeService:
        self.create_calls.append((image, kwargs))
        service = FakeService(kwargs["name"])
        self.items[kwargs["name"]] = service
        return service

    def list(self, **kwargs) -> list[FakeService]:
        return list(self.items.values())


class FakeDocker:
    def __init__(self) -> None:
        self.networks = FakeNetworks()
        self.services = FakeServices()


def test_swarm_adapter_creates_namespaced_network_and_service_from_compose() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter("unix:///var/run/docker.sock", docker_client=docker)
    compose = {
        "services": {
            "api": {
                "image": "harbor.example/apps/api:build-42",
                "command": ["serve", "--port", "80"],
                "working_dir": "/srv/app",
                "environment": {"DEBUG": False, "LOG_LEVEL": "info"},
                "ports": ["8080:80", {"target": 443, "published": 8443, "mode": "ingress"}],
                "volumes": ["api-data:/var/lib/api"],
                "labels": {"team": "apps"},
                "restart": "always",
                "deploy": {"replicas": 2},
            }
        }
    }

    deployment = adapter.deploy("demo", compose)

    assert deployment.appid == "demo"
    assert deployment.network_name == "orchestrator-demo"
    assert docker.networks.create_calls == [
        (
            "orchestrator-demo",
            {
                "driver": "overlay",
                "attachable": True,
                "labels": {"io.docker-jenkins-orchestrator.appid": "demo"},
            },
        )
    ]
    assert len(deployment.services) == 1
    service = deployment.services[0]
    assert service.action == "created"
    assert service.service_name == "demo-api"
    assert service.service_id == "service-demo-api-id"
    assert service.endpoint == "demo-api:8080"
    assert [(port.published_port, port.target_port) for port in service.ports] == [(8080, 80), (8443, 443)]

    image, kwargs = docker.services.create_calls[0]
    assert image == "harbor.example/apps/api:build-42"
    assert kwargs["name"] == "demo-api"
    assert kwargs["env"] == ["DEBUG=false", "LOG_LEVEL=info"]
    assert kwargs["command"] == ["serve", "--port", "80"]
    assert kwargs["workdir"] == "/srv/app"
    assert kwargs["mounts"] == ["api-data:/var/lib/api"]
    assert kwargs["labels"] == {
        "io.docker-jenkins-orchestrator.appid": "demo",
        "io.docker-jenkins-orchestrator.compose-service": "api",
        "team": "apps",
    }
    assert kwargs["networks"] == ["network-orchestrator-demo-id"]
    assert kwargs["endpoint_spec"] == {
        "Mode": "vip",
        "Ports": [
            {"Protocol": "tcp", "TargetPort": 80, "PublishMode": "ingress", "PublishedPort": 8080},
            {"Protocol": "tcp", "TargetPort": 443, "PublishMode": "ingress", "PublishedPort": 8443},
        ],
    }
    assert kwargs["mode"] == {"replicated": {"Replicas": 2}}
    assert kwargs["restart_policy"] == {"Condition": "any"}

    record = deployment.to_models("build-42")[0]
    assert record.build_id == "build-42"
    assert record.status == "deployed"


def test_swarm_adapter_updates_existing_service_without_double_namespacing() -> None:
    docker = FakeDocker()
    docker.networks.items["orchestrator-demo"] = FakeNetwork("orchestrator-demo")
    existing = FakeService("demo-api")
    docker.services.items["demo-api"] = existing
    adapter = DockerSwarmAdapter(docker_client=docker)

    deployment = adapter.deploy(
        "demo",
        {"services": {"demo-api": {"image": "harbor.example/apps/api:build-43", "ports": ["8081:80"]}}},
    )

    assert deployment.services[0].action == "updated"
    assert deployment.services[0].service_name == "demo-api"
    assert docker.services.create_calls == []
    assert existing.update_calls == [
        {
            "image": "harbor.example/apps/api:build-43",
            "env": [],
            "labels": {
                "io.docker-jenkins-orchestrator.appid": "demo",
                "io.docker-jenkins-orchestrator.compose-service": "demo-api",
            },
            "networks": ["network-orchestrator-demo-id"],
            "endpoint_spec": {
                "Mode": "vip",
                "Ports": [
                    {"Protocol": "tcp", "TargetPort": 80, "PublishMode": "ingress", "PublishedPort": 8081}
                ],
            },
        }
    ]


def test_swarm_adapter_rejects_unsupported_host_ip_port_binding_before_service_creation() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError, match="host-IP"):
        adapter.deploy(
            "demo",
            {"services": {"api": {"image": "harbor.example/apps/api:build-44", "ports": ["127.0.0.1:8080:80"]}}},
        )

    assert docker.services.create_calls == []


def test_swarm_adapter_rejects_relative_bind_mount_before_side_effects() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError, match="relative bind"):
        adapter.deploy(
            "demo",
            {"services": {"api": {"image": "example/api:1", "volumes": ["./data:/data"]}}},
        )

    assert docker.networks.create_calls == []
    assert docker.services.create_calls == []


@pytest.mark.parametrize(
    ("compose", "error"),
    [
        ({"services": {"api": {}}}, "must define an image"),
        ({"services": {"api/name": {"image": "example/api:1"}}}, "service name is invalid"),
        (
            {
                "services": {
                    "api": {"image": "example/api:1", "depends_on": ["database"]},
                    "database": {"image": "postgres:16", "depends_on": ["api"]},
                }
            },
            "contains a cycle",
        ),
    ],
)
def test_swarm_adapter_rejects_invalid_compose_before_side_effects(compose: dict, error: str) -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError, match=error):
        adapter.deploy("demo", compose)

    assert docker.networks.create_calls == []
    assert docker.services.create_calls == []


def test_swarm_adapter_orders_dependencies_and_removes_stale_services() -> None:
    docker = FakeDocker()
    stale = FakeService("demo-old")
    docker.services.items[stale.name] = stale
    adapter = DockerSwarmAdapter(docker_client=docker)

    deployment = adapter.deploy(
        "demo",
        {
            "services": {
                "api": {
                    "image": "harbor.example/apps/api:build-45",
                    "depends_on": {"database": {"condition": "service_healthy"}},
                },
                "database": {
                    "image": "postgres:16",
                    "healthcheck": {"test": ["CMD", "pg_isready"]},
                    "deploy": {"resources": {"limits": {"cpus": "1.5", "memory": "512mb"}}},
                },
            }
        },
    )

    assert [image for image, _ in docker.services.create_calls] == ["postgres:16", "harbor.example/apps/api:build-45"]
    assert stale.remove_calls == 1
    database_kwargs = docker.services.create_calls[0][1]
    assert database_kwargs["resources"] == {
        "Limits": {"NanoCPUs": 1_500_000_000, "MemoryBytes": 536_870_912}
    }
    assert [service.service_name for service in deployment.services] == ["demo-database", "demo-api"]


def test_swarm_adapter_accepts_standard_external_volume_metadata() -> None:
    compose = {
        "version": "3.9",
        "services": {"api": {"image": "example/api:1", "volumes": ["shared:/data"]}},
        "volumes": {"shared": {"name": "shared-data", "external": True}},
    }
    DockerSwarmAdapter.validate_compose(compose)

    docker = FakeDocker()
    DockerSwarmAdapter(docker_client=docker).deploy("demo", compose)
    assert docker.services.create_calls[0][1]["mounts"] == ["shared-data:/data"]
