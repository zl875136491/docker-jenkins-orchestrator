from pathlib import Path
from hashlib import sha256

import pytest

from orchestrator.docker_services import DockerServiceError, DockerSwarmAdapter
from orchestrator.naming import external_namespace, managed_volume_name, template_volume_alias
from orchestrator.templates import TemplateCatalog


class NotFound(Exception):
    status_code = 404


class FakeNetwork:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = f"network-{name}-id"
        appid = name.rsplit("-", 1)[-1]
        self.attrs = {
            "ID": self.id,
            "Name": name,
            "Labels": {
                "io.docker-jenkins-orchestrator.appid": appid,
                "io.docker-jenkins-orchestrator.namespace": external_namespace(appid),
            },
        }
        self.remove_calls = 0

    def remove(self) -> None:
        self.remove_calls += 1


class FakeNetworks:
    def __init__(self) -> None:
        self.items: dict[str, FakeNetwork] = {}
        self.get_calls: list[str] = []
        self.create_calls: list[tuple[str, dict]] = []

    def get(self, name: str) -> FakeNetwork:
        self.get_calls.append(name)
        if name not in self.items:
            for network in self.items.values():
                if network.id == name:
                    return network
            raise NotFound(name)
        return self.items[name]

    def create(self, name: str, **kwargs) -> FakeNetwork:
        self.create_calls.append((name, kwargs))
        network = FakeNetwork(name)
        network.attrs["Labels"] = dict(kwargs.get("labels", {}))
        self.items[name] = network
        return network


class FakeService:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = f"service-{name}-id"
        appid = name.rsplit("-", 1)[0]
        self.attrs = {
            "ID": self.id,
            "Spec": {
                "Name": name,
                "Labels": {
                    "io.docker-jenkins-orchestrator.appid": appid,
                    "io.docker-jenkins-orchestrator.namespace": external_namespace(appid),
                },
            },
        }
        self.update_calls: list[dict] = []
        self.remove_calls = 0

    def update(self, **kwargs) -> None:
        self.update_calls.append(kwargs)

    def remove(self) -> None:
        self.remove_calls += 1


class TaskService(FakeService):
    def __init__(self, name: str, states: list[str], replicas: int = 1) -> None:
        super().__init__(name)
        self.attrs["Spec"] = {"Mode": {"Replicated": {"Replicas": replicas}}}
        self.states = states

    def tasks(self) -> list[dict]:
        return [{"DesiredState": "running", "Status": {"State": state}} for state in self.states]


class FakeServices:
    def __init__(self) -> None:
        self.items: dict[str, FakeService] = {}
        self.get_calls: list[str] = []
        self.create_calls: list[tuple[str, dict]] = []

    def get(self, name: str) -> FakeService:
        self.get_calls.append(name)
        if name not in self.items:
            for service in self.items.values():
                if service.id == name:
                    return service
            raise NotFound(name)
        return self.items[name]

    def create(self, image: str, **kwargs) -> FakeService:
        self.create_calls.append((image, kwargs))
        service = FakeService(kwargs["name"])
        service.attrs["Spec"]["Labels"] = dict(kwargs.get("labels", {}))
        mounts = []
        for mount in kwargs.get("mounts", []):
            if isinstance(mount, str):
                source, target = mount.split(":")[:2]
                mounts.append({"Type": "bind" if source.startswith("/") else "volume", "Source": source, "Target": target})
            elif isinstance(mount, dict):
                mounts.append(dict(mount))
        service.attrs["Spec"]["TaskTemplate"] = {"ContainerSpec": {"Mounts": mounts}}
        self.items[kwargs["name"]] = service
        return service

    def list(self, **kwargs) -> list[FakeService]:
        services = list(self.items.values())
        filters = kwargs.get("filters")
        if isinstance(filters, dict):
            label = filters.get("label")
            if isinstance(label, str) and "=" in label:
                key, expected = label.split("=", 1)
                services = [
                    service
                    for service in services
                    if str(service.attrs.get("Spec", {}).get("Labels", {}).get(key)) == expected
                ]
        return services


class FakeDocker:
    def __init__(self) -> None:
        self.networks = FakeNetworks()
        self.services = FakeServices()


class DockerApiFailure(Exception):
    def __init__(self, explanation: str) -> None:
        super().__init__(explanation)
        self.explanation = explanation


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
                "labels": {
                    "io.docker-jenkins-orchestrator.appid": "demo",
                    "io.docker-jenkins-orchestrator.namespace": "demo",
                },
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
    assert kwargs["mounts"] == [f"{managed_volume_name('demo', 'api-data')}:/var/lib/api"]
    assert kwargs["labels"] == {
        "io.docker-jenkins-orchestrator.appid": "demo",
        "io.docker-jenkins-orchestrator.namespace": "demo",
        "io.docker-jenkins-orchestrator.compose-service": "api",
        "team": "apps",
    }
    assert kwargs["networks"] == [{"Target": "network-orchestrator-demo-id", "Aliases": ["api"]}]
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
    assert [port.model_dump() for port in record.published_ports] == [
        {"target_port": 80, "published_port": 8080, "protocol": "tcp", "mode": "ingress"},
        {"target_port": 443, "published_port": 8443, "protocol": "tcp", "mode": "ingress"},
    ]


def test_swarm_adapter_reassigns_conflicting_and_dynamic_published_ports() -> None:
    docker = FakeDocker()
    occupied = FakeService("other-api")
    occupied.attrs["Endpoint"] = {
        "Ports": [{"PublishedPort": 18000, "TargetPort": 80, "Protocol": "tcp", "PublishMode": "ingress"}]
    }
    docker.services.items[occupied.name] = occupied
    adapter = DockerSwarmAdapter(docker_client=docker, published_port_range_start=18000, published_port_range_end=18002)

    deployment = adapter.deploy(
        "demo",
        {"services": {"api": {"image": "example/api:1", "ports": ["18000:80", "81"]}}},
    )

    ports = deployment.services[0].ports
    assert [(port.published_port, port.target_port) for port in ports] == [(18001, 80), (18002, 81)]
    endpoint_ports = docker.services.create_calls[0][1]["endpoint_spec"]["Ports"]
    assert [port["PublishedPort"] for port in endpoint_ports] == [18001, 18002]


def test_swarm_adapter_allows_same_published_number_for_different_protocol() -> None:
    docker = FakeDocker()
    occupied = FakeService("other-api")
    occupied.attrs["Endpoint"] = {
        "Ports": [{"PublishedPort": 18000, "TargetPort": 80, "Protocol": "udp", "PublishMode": "ingress"}]
    }
    docker.services.items[occupied.name] = occupied
    adapter = DockerSwarmAdapter(docker_client=docker, published_port_range_start=18000, published_port_range_end=18000)

    deployment = adapter.deploy(
        "demo",
        {"services": {"api": {"image": "example/api:1", "ports": [{"target": 80, "published": 18000, "protocol": "tcp"}]}}},
    )

    assert deployment.services[0].ports[0].published_port == 18000


def test_swarm_adapter_reports_published_port_range_exhaustion() -> None:
    docker = FakeDocker()
    occupied = FakeService("other-api")
    occupied.attrs["Endpoint"] = {
        "Ports": [{"PublishedPort": 18000, "TargetPort": 80, "Protocol": "tcp", "PublishMode": "ingress"}]
    }
    docker.services.items[occupied.name] = occupied
    adapter = DockerSwarmAdapter(docker_client=docker, published_port_range_start=18000, published_port_range_end=18000)

    with pytest.raises(DockerServiceError, match="No available published port"):
        adapter.deploy("demo", {"services": {"api": {"image": "example/api:1", "ports": ["18000:80"]}}})


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
                "io.docker-jenkins-orchestrator.namespace": "demo",
                "io.docker-jenkins-orchestrator.compose-service": "demo-api",
            },
            "networks": [{"Target": "network-orchestrator-demo-id", "Aliases": ["demo-api"]}],
            "endpoint_spec": {
                "Mode": "vip",
                "Ports": [
                    {"Protocol": "tcp", "TargetPort": 80, "PublishMode": "ingress", "PublishedPort": 8081}
                ],
            },
        }
    ]


def test_swarm_adapter_does_not_adopt_legacy_lowercase_service_for_uppercase_appid() -> None:
    docker = FakeDocker()
    docker.networks.items["orchestrator-demo"] = FakeNetwork("orchestrator-demo")
    existing = FakeService("demo-api")
    existing.attrs["Endpoint"] = {
        "Ports": [{"PublishedPort": 18000, "TargetPort": 80, "Protocol": "tcp", "PublishMode": "ingress"}]
    }
    docker.services.items[existing.name] = existing
    adapter = DockerSwarmAdapter(docker_client=docker, published_port_range_start=18000, published_port_range_end=18001)

    deployment = adapter.deploy("Demo", {"services": {"api": {"image": "example/api:2", "ports": ["18000:80"]}}})

    assert deployment.services[0].service_name == "appid-v1-44656d6f-api"
    assert deployment.services[0].ports[0].published_port == 18001
    assert existing.update_calls == []
    assert docker.networks.create_calls[0][0] == "orchestrator-appid-v1-44656d6f"


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


def test_swarm_adapter_preserves_safe_docker_service_error_context() -> None:
    docker = FakeDocker()

    def fail_create(_image: str, **_kwargs):
        raise DockerApiFailure("registry https://user:password@example.invalid/v2: network allocator state missing")

    docker.services.create = fail_create
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError) as error:
        adapter.deploy("demo", {"services": {"api": {"image": "example/api:1"}}})

    message = str(error.value)
    assert "Unable to create Docker service demo-api" in message
    assert "network allocator state missing" in message
    assert "password" not in message
    assert "***:***@" in message


def test_swarm_adapter_preserves_safe_docker_network_error_context() -> None:
    docker = FakeDocker()

    def fail_network(_name: str, **_kwargs):
        raise DockerApiFailure("could not find network allocator state")

    docker.networks.create = fail_network
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError, match="Unable to create Docker network orchestrator-demo") as error:
        adapter.deploy("demo", {"services": {"api": {"image": "example/api:1"}}})

    assert "network allocator state" in str(error.value)


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


def test_swarm_adapter_shortens_long_app_service_names_stably() -> None:
    docker = FakeDocker()
    appid = "linkwarden-" + "a" * 32
    adapter = DockerSwarmAdapter(network_name="livee2e", docker_client=docker)

    deployment = adapter.deploy(
        appid,
        {
            "services": {
                "linkwarden-meilisearch": {"image": "example/meili:1"},
                "linkwarden-postgres": {"image": "example/postgres:1"},
            }
        },
    )

    names = [service.service_name for service in deployment.services]
    assert all(len(name) <= 63 for name in names)
    assert names[0] != names[1]
    assert all(name.startswith(appid[:20]) for name in names)
    assert deployment.network_name == f"livee2e-{appid}"

    second = DockerSwarmAdapter(network_name="livee2e", docker_client=FakeDocker()).deploy(
        appid,
        {
            "services": {
                "linkwarden-meilisearch": {"image": "example/meili:1"},
                "linkwarden-postgres": {"image": "example/postgres:1"},
            }
        },
    )
    assert [service.service_name for service in second.services] == names


def test_swarm_adapter_waits_for_real_service_tasks_to_run() -> None:
    docker = FakeDocker()
    service = TaskService("demo-api", ["running"], replicas=2)
    docker.services.items[service.name] = service
    adapter = DockerSwarmAdapter(docker_client=docker, readiness_timeout_seconds=0, readiness_poll_interval_seconds=0.01)

    with pytest.raises(DockerServiceError, match="did not reach running state"):
        adapter._wait_for_service_ready(docker, "demo-api")

    service.states = ["running", "running"]
    adapter._wait_for_service_ready(docker, "demo-api")


def test_swarm_adapter_reports_failed_task_after_readiness_timeout() -> None:
    docker = FakeDocker()
    service = TaskService("demo-api", ["failed"])
    docker.services.items[service.name] = service
    adapter = DockerSwarmAdapter(docker_client=docker, readiness_timeout_seconds=0, readiness_poll_interval_seconds=0.01)

    with pytest.raises(DockerServiceError, match="failed"):
        adapter._wait_for_service_ready(docker, "demo-api")


def test_swarm_adapter_rolls_back_new_service_and_network_after_readiness_failure() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)

    def fail_readiness(_client: object, _service_name: str) -> None:
        raise DockerServiceError("readiness failed")

    adapter._wait_for_service_ready = fail_readiness

    with pytest.raises(DockerServiceError, match="readiness failed"):
        adapter.deploy("demo", {"services": {"api": {"image": "example/api:1"}}})

    created = docker.services.items["demo-api"]
    network = docker.networks.items["orchestrator-demo"]
    assert created.remove_calls == 1
    assert network.remove_calls == 1


def test_swarm_adapter_rollback_keeps_updated_services_and_network_with_other_service() -> None:
    docker = FakeDocker()
    existing = FakeService("demo-existing")
    docker.services.items[existing.name] = existing
    adapter = DockerSwarmAdapter(docker_client=docker)

    def fail_new_service(_client: object, service_name: str) -> None:
        if service_name == "demo-new":
            raise DockerServiceError("new service readiness failed")

    adapter._wait_for_service_ready = fail_new_service

    with pytest.raises(DockerServiceError, match="new service readiness failed"):
        adapter.deploy(
            "demo",
            {
                "services": {
                    "existing": {"image": "example/existing:2"},
                    "new": {"image": "example/new:1"},
                }
            },
        )

    assert existing.remove_calls == 0
    assert docker.services.items["demo-new"].remove_calls == 1
    assert docker.networks.items["orchestrator-demo"].remove_calls == 0


def test_swarm_adapter_ignores_running_tasks_marked_for_shutdown() -> None:
    docker = FakeDocker()
    service = TaskService("demo-api", ["running"])
    docker.services.items[service.name] = service
    service.tasks = lambda: [
        {"DesiredState": "shutdown", "Status": {"State": "running"}},
        {"DesiredState": "running", "Status": {"State": "pending"}},
    ]
    adapter = DockerSwarmAdapter(docker_client=docker, readiness_timeout_seconds=0, readiness_poll_interval_seconds=0.01)

    with pytest.raises(DockerServiceError, match="shutdown:running"):
        adapter._wait_for_service_ready(docker, "demo-api")


def test_swarm_adapter_managed_labels_cannot_be_overridden_by_compose() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)

    adapter.deploy(
        "demo",
        {
            "services": {
                "api": {
                    "image": "example/api:1",
                    "labels": {
                        "io.docker-jenkins-orchestrator.appid": "attacker",
                        "io.docker-jenkins-orchestrator.namespace": "attacker",
                        "io.docker-jenkins-orchestrator.compose-service": "other",
                    },
                }
            }
        },
    )

    labels = docker.services.create_calls[0][1]["labels"]
    assert labels["io.docker-jenkins-orchestrator.appid"] == "demo"
    assert labels["io.docker-jenkins-orchestrator.namespace"] == "demo"
    assert labels["io.docker-jenkins-orchestrator.compose-service"] == "api"


def test_swarm_adapter_separates_case_distinct_appids() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)

    upper = adapter.deploy("Foo", {"services": {"api": {"image": "example/api:1"}}})
    lower = adapter.deploy("foo", {"services": {"api": {"image": "example/api:1"}}})

    assert upper.network_name == "orchestrator-appid-v1-466f6f"
    assert lower.network_name == "orchestrator-foo"
    assert upper.services[0].service_name == "appid-v1-466f6f-api"
    assert lower.services[0].service_name == "foo-api"
    assert upper.network_name != lower.network_name
    assert upper.services[0].service_name != lower.services[0].service_name


def test_swarm_adapter_fails_closed_for_unowned_existing_network() -> None:
    docker = FakeDocker()
    network = FakeNetwork("orchestrator-demo")
    network.attrs["Labels"] = {}
    docker.networks.items[network.name] = network
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError, match="ownership could not be verified"):
        adapter.deploy("demo", {"services": {"api": {"image": "example/api:1"}}})

    assert docker.services.create_calls == []


def test_swarm_adapter_fails_closed_for_unowned_existing_service() -> None:
    docker = FakeDocker()
    existing = FakeService("demo-api")
    existing.attrs["Spec"]["Labels"] = {}
    docker.services.items[existing.name] = existing
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError, match="ownership could not be verified"):
        adapter.deploy("demo", {"services": {"api": {"image": "example/api:1"}}})

    assert existing.update_calls == []


def test_swarm_adapter_scopes_implicit_volumes_and_preserves_explicit_shared_names() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)
    adapter.deploy(
        "demo",
        {
            "services": {
                "api": {
                    "image": "example/api:1",
                    "volumes": ["private:/private", "shared:/shared"],
                }
            },
            "volumes": {
                "private": {},
                "shared": {"name": "platform-shared", "external": True},
            },
        },
    )

    assert docker.services.create_calls[0][1]["mounts"] == [
        f"{managed_volume_name('demo', 'private')}:/private",
        "platform-shared:/shared",
    ]


def test_service_physical_name_collision_fails_before_docker_client_creation() -> None:
    docker = FakeDocker()
    client_factory_calls: list[str | None] = []

    def client_factory(base_url: str | None) -> FakeDocker:
        client_factory_calls.append(base_url)
        return docker

    adapter = DockerSwarmAdapter(client_factory=client_factory)

    with pytest.raises(DockerServiceError, match="map to the same Docker service name foo-api"):
        adapter.deploy(
            "foo", {"services": {"api": {"image": "example/api:1"}, "foo-api": {"image": "example/api:2"}}}
        )

    assert client_factory_calls == []
    assert docker.networks.create_calls == []
    assert docker.services.create_calls == []


@pytest.mark.parametrize("declared", [True, False])
def test_simulated_managed_volume_collision_fails_before_docker_client_creation(
    monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    docker = FakeDocker()
    client_factory_calls: list[str | None] = []

    def client_factory(base_url: str | None) -> FakeDocker:
        client_factory_calls.append(base_url)
        return docker

    monkeypatch.setattr("orchestrator.docker_services.managed_volume_name", lambda namespace, alias: "same-volume")
    compose = {"services": {"api": {"image": "example/api:1", "volumes": ["data:/data", "foo-data:/other"]}}}
    if declared:
        compose["volumes"] = {"data": {}, "foo-data": {}}

    with pytest.raises(DockerServiceError, match="map to the same managed Docker volume name same-volume"):
        DockerSwarmAdapter(client_factory=client_factory).deploy("foo", compose)

    assert client_factory_calls == []
    assert docker.networks.create_calls == []
    assert docker.services.create_calls == []


@pytest.mark.parametrize("declared", [True, False])
def test_invalid_implicit_volume_alias_is_a_docker_service_error_before_client_creation(declared: bool) -> None:
    docker = FakeDocker()
    client_factory_calls: list[str | None] = []

    def client_factory(base_url: str | None) -> FakeDocker:
        client_factory_calls.append(base_url)
        return docker

    compose = {"services": {"api": {"image": "example/api:1", "volumes": ["invalid/name:/data"]}}}
    if declared:
        compose["volumes"] = {"invalid/name": {}}

    with pytest.raises(DockerServiceError, match="invalid for managed Docker volume naming"):
        DockerSwarmAdapter(client_factory=client_factory).deploy("foo", compose)

    assert client_factory_calls == []
    assert docker.networks.create_calls == []
    assert docker.services.create_calls == []


@pytest.mark.parametrize("sharing_boundary", ["name", "external", "both"])
def test_explicit_volume_can_intentionally_share_managed_physical_name(sharing_boundary: str) -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)
    physical_name = managed_volume_name("foo", "data")
    shared_alias = physical_name if sharing_boundary == "external" else "shared"
    shared_definition = {}
    if sharing_boundary in {"name", "both"}:
        shared_definition["name"] = physical_name
    if sharing_boundary in {"external", "both"}:
        shared_definition["external"] = True

    adapter.deploy(
        "foo",
        {
            "services": {"api": {"image": "example/api:1", "volumes": ["data:/data", f"{shared_alias}:/shared"]}},
            "volumes": {"data": {}, shared_alias: shared_definition},
        },
    )

    assert docker.services.create_calls[0][1]["mounts"] == [f"{physical_name}:/data", f"{physical_name}:/shared"]


@pytest.mark.parametrize("declared", [True, False])
def test_different_aliases_under_one_app_have_distinct_managed_volumes(declared: bool) -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)
    compose = {
        "services": {
            "api": {
                "image": "example/api:1",
                "volumes": ["data:/data", {"source": "foo-data", "target": "/other"}],
            }
        }
    }
    if declared:
        compose["volumes"] = {"data": {}, "foo-data": {}}

    deployment = adapter.deploy("foo", compose)
    physical_data = managed_volume_name("foo", "data")
    physical_prefixed_data = managed_volume_name("foo", "foo-data")

    assert physical_data != physical_prefixed_data
    assert docker.services.create_calls[0][1]["mounts"] == [
        f"{physical_data}:/data", f"{physical_prefixed_data}:/other:rw"
    ]
    assert adapter.deploy("foo", compose).services[0].service_name == deployment.services[0].service_name
    assert docker.services.items[deployment.services[0].service_name].update_calls[0]["mounts"] == [
        f"{physical_data}:/data", f"{physical_prefixed_data}:/other:rw"
    ]


@pytest.mark.parametrize("declared", [True, False])
def test_hyphenated_app_and_volume_alias_boundaries_do_not_share_managed_volume(declared: bool) -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)

    for appid, alias in [("foo", "bar-data"), ("foo-bar", "data")]:
        compose = {"services": {"api": {"image": "example/api:1", "volumes": [f"{alias}:/data"]}}}
        if declared:
            compose["volumes"] = {alias: {}}
        adapter.deploy(appid, compose)

    first_physical_name = managed_volume_name("foo", "bar-data")
    second_physical_name = managed_volume_name("foo-bar", "data")
    assert first_physical_name != second_physical_name
    assert docker.services.create_calls[0][1]["mounts"] == [f"{first_physical_name}:/data"]
    assert docker.services.create_calls[1][1]["mounts"] == [f"{second_physical_name}:/data"]
    assert docker.services.items["foo-api"].remove_calls == 0
    assert docker.services.items["foo-bar-api"].remove_calls == 0


def test_swarm_adapter_long_resource_names_keep_complete_hash_suffix() -> None:
    adapter = DockerSwarmAdapter(network_name="network" + "x" * 50)
    namespace = adapter._namespace("A" + "b" * 63)
    network_name = adapter._network_name(namespace)
    service_name = adapter._service_name(namespace, "api" + "z" * 80)

    assert len(network_name) <= 63
    assert len(service_name) <= 63
    assert network_name[-11] == "-"
    assert service_name[-11] == "-"
    assert all(character in "0123456789abcdef" for character in network_name[-10:])
    assert all(character in "0123456789abcdef" for character in service_name[-10:])


@pytest.mark.parametrize(
    "owner_labels",
    [
        {},
        {"io.docker-jenkins-orchestrator.appid": "Foo"},
        {
            "io.docker-jenkins-orchestrator.appid": "Foo",
            "io.docker-jenkins-orchestrator.namespace": "foo",
        },
        {
            "io.docker-jenkins-orchestrator.appid": "foo",
            "io.docker-jenkins-orchestrator.namespace": "appid-v1-466f6f",
        },
    ],
)
def test_encoded_app_rejects_network_without_exact_raw_owner_and_namespace(owner_labels: dict) -> None:
    docker = FakeDocker()
    network = FakeNetwork("orchestrator-appid-v1-466f6f")
    network.attrs["Labels"] = owner_labels
    docker.networks.items[network.name] = network

    with pytest.raises(DockerServiceError, match="ownership could not be verified"):
        DockerSwarmAdapter(docker_client=docker).deploy("Foo", {"services": {"api": {"image": "example/api:1"}}})

    assert docker.services.create_calls == []
    assert network.remove_calls == 0


@pytest.mark.parametrize("original_appid", ["Foo", "foo"])
def test_canonical_app_rejects_ambiguous_legacy_lowercase_owner_labels(original_appid: str) -> None:
    docker = FakeDocker()
    # This is what HEAD deploy("Foo", ...) actually persisted: lowercasing
    # happens before both _ensure_network and _labels receive the appid. The
    # same label was also written for a genuine lowercase "foo" application.
    legacy_labels = {"io.docker-jenkins-orchestrator.appid": original_appid.lower()}
    network = FakeNetwork("orchestrator-foo")
    network.attrs["Labels"] = dict(legacy_labels)
    service = FakeService("foo-api")
    service.attrs["Spec"]["Labels"] = dict(legacy_labels)
    stale = FakeService("foo-old")
    stale.attrs["Spec"]["Labels"] = dict(legacy_labels)
    docker.networks.items[network.name] = network
    docker.services.items[service.name] = service
    docker.services.items[stale.name] = stale
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError, match="migrate legacy resources explicitly"):
        adapter.deploy("foo", {"services": {"api": {"image": "example/api:2"}}})

    assert docker.networks.create_calls == []
    assert docker.services.create_calls == []
    assert service.update_calls == []
    assert network.remove_calls == service.remove_calls == stale.remove_calls == 0

    # The uppercase application can instead deploy safely to its new encoded
    # namespace without claiming or cleaning up any of these ambiguous ones.
    isolated = adapter.deploy("Foo", {"services": {"api": {"image": "example/api:2"}}})
    assert isolated.network_name == "orchestrator-appid-v1-466f6f"
    assert isolated.services[0].service_name == "appid-v1-466f6f-api"
    assert service.update_calls == []
    assert network.remove_calls == service.remove_calls == stale.remove_calls == 0


def test_canonical_app_rejects_ambiguous_legacy_service_even_with_verified_network() -> None:
    docker = FakeDocker()
    network = FakeNetwork("orchestrator-foo")
    service = FakeService("foo-api")
    service.attrs["Spec"]["Labels"] = {"io.docker-jenkins-orchestrator.appid": "foo"}
    docker.networks.items[network.name] = network
    docker.services.items[service.name] = service

    with pytest.raises(DockerServiceError, match="migrate legacy resources explicitly"):
        DockerSwarmAdapter(docker_client=docker).deploy("foo", {"services": {"api": {"image": "example/api:2"}}})

    assert service.update_calls == []
    assert network.remove_calls == service.remove_calls == 0


def test_canonical_app_rejects_ambiguous_legacy_stale_cleanup() -> None:
    docker = FakeDocker()
    network = FakeNetwork("orchestrator-foo")
    stale = FakeService("foo-old")
    stale.attrs["Spec"]["Labels"] = {"io.docker-jenkins-orchestrator.appid": "foo"}
    docker.networks.items[network.name] = network
    docker.services.items[stale.name] = stale

    with pytest.raises(DockerServiceError, match="migrate legacy resources explicitly"):
        DockerSwarmAdapter(docker_client=docker).deploy("foo", {"services": {"api": {"image": "example/api:2"}}})

    assert stale.remove_calls == 0
    assert network.remove_calls == 0
    assert docker.services.items["foo-api"].remove_calls == 1


def test_explicitly_verified_legacy_resource_labels_allow_update_without_renaming() -> None:
    docker = FakeDocker()
    network = FakeNetwork("orchestrator-foo")
    network.attrs["Labels"] = {"io.docker-jenkins-orchestrator.appid": "foo"}
    service = FakeService("foo-api")
    service.attrs["Spec"]["Labels"] = {"io.docker-jenkins-orchestrator.appid": "foo"}
    docker.networks.items[network.name] = network
    docker.services.items[service.name] = service
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError, match="ownership could not be verified"):
        adapter.deploy("foo", {"services": {"api": {"image": "example/api:2"}}})

    # Simulate an explicit migration only after the raw owner was independently
    # verified. The adapter never writes these labels to an unknown resource.
    verified_labels = {
        "io.docker-jenkins-orchestrator.appid": "foo",
        "io.docker-jenkins-orchestrator.namespace": "foo",
    }
    network.attrs["Labels"] = dict(verified_labels)
    service.attrs["Spec"]["Labels"] = dict(verified_labels)

    deployment = adapter.deploy("foo", {"services": {"api": {"image": "example/api:2"}}})

    assert deployment.network_name == "orchestrator-foo"
    assert deployment.services[0].service_name == "foo-api"
    assert deployment.services[0].action == "updated"
    assert service.update_calls[0]["labels"]["io.docker-jenkins-orchestrator.namespace"] == "foo"
    assert docker.networks.create_calls == docker.services.create_calls == []


def test_stale_cleanup_validates_all_owners_before_removing_previous_services() -> None:
    docker = FakeDocker()
    stale = FakeService("demo-old")
    invalid = FakeService("demo-foreign")
    invalid.attrs["Spec"]["Labels"]["io.docker-jenkins-orchestrator.namespace"] = "another-namespace"
    docker.services.items[stale.name] = stale
    docker.services.items[invalid.name] = invalid
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError, match="ownership could not be verified"):
        adapter.deploy("demo", {"services": {"api": {"image": "example/api:1"}}})

    assert stale.remove_calls == 0
    assert invalid.remove_calls == 0
    assert docker.services.items["demo-api"].remove_calls == 1


def test_rollback_does_not_remove_replacement_service_or_network_with_same_name() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)
    originals = []

    def replace_then_fail(_client: object, service_name: str) -> None:
        original_service = docker.services.items[service_name]
        original_network = docker.networks.items["orchestrator-demo"]
        originals.extend([original_service, original_network])
        replacement_service = FakeService(service_name)
        replacement_service.id = "replacement-service-id"
        replacement_network = FakeNetwork("orchestrator-demo")
        replacement_network.id = "replacement-network-id"
        docker.services.items[service_name] = replacement_service
        docker.networks.items[replacement_network.name] = replacement_network
        raise DockerServiceError("readiness failed after resources were replaced")

    adapter._wait_for_service_ready = replace_then_fail

    with pytest.raises(DockerServiceError, match="readiness failed"):
        adapter.deploy("demo", {"services": {"api": {"image": "example/api:1"}}})

    assert docker.services.items["demo-api"].remove_calls == 0
    assert docker.networks.items["orchestrator-demo"].remove_calls == 0
    assert all(resource.remove_calls == 0 for resource in originals)


def test_rollback_leaves_new_resource_when_owner_changes_during_rollout() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)

    def change_owner_then_fail(_client: object, service_name: str) -> None:
        docker.services.items[service_name].attrs["Spec"]["Labels"]["io.docker-jenkins-orchestrator.appid"] = "other"
        raise DockerServiceError("readiness failed")

    adapter._wait_for_service_ready = change_owner_then_fail
    with pytest.raises(DockerServiceError, match="readiness failed"):
        adapter.deploy("demo", {"services": {"api": {"image": "example/api:1"}}})

    assert docker.services.items["demo-api"].remove_calls == 0
    assert docker.networks.items["orchestrator-demo"].remove_calls == 0


@pytest.mark.parametrize("historical_name", ["data", "demo-data"])
def test_existing_volume_is_not_rebound_without_explicit_preservation(historical_name: str) -> None:
    docker = FakeDocker()
    service = FakeService("demo-api")
    service.attrs["Spec"]["TaskTemplate"] = {
        "ContainerSpec": {"Mounts": [{"Type": "volume", "Source": historical_name, "Target": "/data"}]}
    }
    docker.services.items[service.name] = service
    adapter = DockerSwarmAdapter(docker_client=docker)

    with pytest.raises(DockerServiceError, match="refusing to rebind historical data"):
        adapter.deploy(
            "demo", {"services": {"api": {"image": "example/api:2", "volumes": ["data:/data"]}}, "volumes": {"data": {}}}
        )

    assert service.update_calls == []
    assert service.remove_calls == 0
    adapter.deploy(
        "demo",
        {
            "services": {"api": {"image": "example/api:2", "volumes": ["data:/data"]}},
            "volumes": {"data": {"name": historical_name, "external": True}},
        },
    )
    assert service.update_calls[0]["mounts"] == [f"{historical_name}:/data"]


def test_case_distinct_apps_do_not_share_default_named_volumes() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)
    compose = {"services": {"api": {"image": "example/api:1", "volumes": ["data:/data"]}}, "volumes": {"data": {}}}

    adapter.deploy("Foo", compose)
    adapter.deploy("foo", compose)

    assert docker.services.create_calls[0][1]["mounts"] == [
        f"{managed_volume_name(external_namespace('Foo'), 'data')}:/data"
    ]
    assert docker.services.create_calls[1][1]["mounts"] == [f"{managed_volume_name('foo', 'data')}:/data"]


def test_deploy_one_implicit_volume_is_scoped_with_complete_app_and_alias_identity() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)

    adapter.deploy_one("foo", "api", {"image": "example/api:1", "volumes": ["bar-data:/data"]})
    adapter.deploy_one("foo-bar", "api", {"image": "example/api:1", "volumes": ["data:/data"]})

    assert docker.services.create_calls[0][1]["mounts"] == [f"{managed_volume_name('foo', 'bar-data')}:/data"]
    assert docker.services.create_calls[1][1]["mounts"] == [f"{managed_volume_name('foo-bar', 'data')}:/data"]


@pytest.mark.parametrize(
    "appids", [("foo", "foo-bar"), ("foo", "Foo"), ("A" * 64, "c822e6a0-2096-4e1d-8915-b5f24c6d9670")]
)
def test_generic_template_volumes_are_app_scoped_and_match_app_bound_template(appids: tuple[str, str]) -> None:
    catalog = TemplateCatalog(Path(__file__).parents[1] / "templates" / "catalog.yaml")
    generic_compose = catalog.compose(["mongodb"])
    alias = next(iter(generic_compose["volumes"]))
    assert alias == template_volume_alias("mongodb", "data")
    assert generic_compose["volumes"][alias] == {}
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)
    physical_names: list[str] = []

    for appid in appids:
        generic_deployment = adapter.deploy(appid, generic_compose)
        service_name = generic_deployment.services[0].service_name
        physical_name = managed_volume_name(external_namespace(appid), alias)
        physical_names.append(physical_name)
        assert docker.services.items[service_name].attrs["Spec"]["TaskTemplate"]["ContainerSpec"]["Mounts"] == [
            {"Type": "volume", "Source": physical_name, "Target": "/data/db"}
        ]

        bound_compose = catalog.compose(["mongodb"], appid=appid)
        assert bound_compose["volumes"] == {physical_name: {"name": physical_name}}
        bound_deployment = adapter.deploy(appid, bound_compose)
        assert bound_deployment.services[0].service_name == service_name
        assert bound_deployment.services[0].action == "updated"
        assert docker.services.items[service_name].update_calls[0]["mounts"] == [f"{physical_name}:/data/db"]

    assert physical_names[0] != physical_names[1]
    assert all(service.remove_calls == 0 for service in docker.services.items.values())


@pytest.mark.parametrize("bound_template", [True, False])
@pytest.mark.parametrize("historical_name", ["mongodb-data", "demo-mongodb-data"])
def test_canonical_template_refuses_to_replace_existing_historical_volume(
    bound_template: bool, historical_name: str
) -> None:
    docker = FakeDocker()
    network = FakeNetwork("orchestrator-demo")
    service = FakeService("demo-mongodb")
    service.attrs["Spec"]["TaskTemplate"] = {
        "ContainerSpec": {"Mounts": [{"Type": "volume", "Source": historical_name, "Target": "/data/db"}]}
    }
    docker.networks.items[network.name] = network
    docker.services.items[service.name] = service
    compose = TemplateCatalog(Path(__file__).parents[1] / "templates" / "catalog.yaml").compose(
        ["mongodb"], appid="demo" if bound_template else None
    )

    with pytest.raises(DockerServiceError, match="refusing to rebind historical data automatically"):
        DockerSwarmAdapter(docker_client=docker).deploy("demo", compose)

    assert service.update_calls == []
    assert service.attrs["Spec"]["TaskTemplate"]["ContainerSpec"]["Mounts"][0]["Source"] == historical_name
    assert service.remove_calls == network.remove_calls == 0
    assert docker.networks.create_calls == []
    assert docker.services.create_calls == []


def test_deploy_one_rolls_back_only_its_new_resources_on_readiness_failure() -> None:
    docker = FakeDocker()
    adapter = DockerSwarmAdapter(docker_client=docker)

    def fail(_client: object, _service_name: str) -> None:
        raise DockerServiceError("readiness failed")

    adapter._wait_for_service_ready = fail
    with pytest.raises(DockerServiceError, match="readiness failed"):
        adapter.deploy_one("demo", "api", {"image": "example/api:1"})

    assert docker.services.items["demo-api"].remove_calls == 1
    assert docker.networks.items["orchestrator-demo"].remove_calls == 1


@pytest.mark.parametrize("appid", ["A" * 64, "c822e6a0-2096-4e1d-8915-b5f24c6d9670"])
def test_template_service_and_volume_names_are_not_prefixed_twice(appid: str) -> None:
    compose = TemplateCatalog(Path(__file__).parents[1] / "templates" / "catalog.yaml").compose(
        ["mongodb"], appid=appid
    )
    service_name = next(iter(compose["services"]))
    volume_name = next(iter(compose["volumes"]))
    docker = FakeDocker()

    adapter = DockerSwarmAdapter(docker_client=docker)
    deployment = adapter.deploy(appid, compose)

    assert deployment.services[0].service_name == service_name
    assert docker.services.create_calls[0][1]["mounts"] == [f"{volume_name}:/data/db"]
    assert docker.services.create_calls[0][1]["labels"]["io.docker-jenkins-orchestrator.appid"] == appid
    repeated = adapter.deploy(appid, compose)
    assert repeated.services[0].service_name == service_name
    assert repeated.services[0].action == "updated"
    assert docker.services.items[service_name].update_calls[0]["mounts"] == [f"{volume_name}:/data/db"]


def test_long_canonical_template_preserves_legacy_swarm_service_name() -> None:
    appid = "a" * 64
    compose = TemplateCatalog(Path(__file__).parents[1] / "templates" / "catalog.yaml").compose(["go"], appid=appid)
    adapter = DockerSwarmAdapter(docker_client=FakeDocker())
    legacy_digest = sha256(f"{appid}:{appid}-go".encode()).hexdigest()[:10]
    expected_legacy_name = f"{legacy_digest}-{appid[:51]}"

    deployment = adapter.deploy(appid, compose)

    assert deployment.services[0].service_name == expected_legacy_name
