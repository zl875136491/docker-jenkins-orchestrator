"""Docker Swarm deployment adapter for rendered Compose service mappings."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Literal

from orchestrator.adapters import AdapterError
from orchestrator.models import DeploymentService


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PORT_PROTOCOLS = {"tcp", "udp", "sctp"}


class DockerServiceError(AdapterError):
    """A Swarm operation failed without including sensitive service values."""


@dataclass(frozen=True)
class PublishedPort:
    """A normalized Swarm service port publication."""

    target_port: int
    published_port: int | None
    protocol: str = "tcp"
    mode: str = "ingress"

    def endpoint_spec(self) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "Protocol": self.protocol,
            "TargetPort": self.target_port,
            "PublishMode": self.mode,
        }
        if self.published_port is not None:
            spec["PublishedPort"] = self.published_port
        return spec


@dataclass(frozen=True)
class ServiceDeployment:
    """The Docker-facing result for a single Compose service."""

    service_id: str
    service_name: str
    image: str
    action: Literal["created", "updated"]
    endpoint: str | None
    ports: tuple[PublishedPort, ...]

    def to_model(self, appid: str, build_id: str, *, status: str = "deployed") -> DeploymentService:
        """Convert this external result into the repository's domain model."""

        return DeploymentService(
            service_id=self.service_id,
            appid=appid,
            build_id=build_id,
            service_name=self.service_name,
            image=self.image,
            status=status,
            endpoint=self.endpoint,
        )


@dataclass(frozen=True)
class SwarmDeployment:
    """Structured outcome for all services in one application deployment."""

    appid: str
    network_name: str
    services: tuple[ServiceDeployment, ...]

    def to_models(self, build_id: str, *, status: str = "deployed") -> list[DeploymentService]:
        return [service.to_model(self.appid, build_id, status=status) for service in self.services]


class DockerSwarmAdapter:
    """Create or update app-namespaced Docker Swarm services.

    The adapter intentionally accepts a high-level Docker SDK client through
    ``docker_client``.  Tests can pass a small fake, and production clients are
    not instantiated until deployment actually begins.
    """

    def __init__(
        self,
        base_url: str | None = None,
        network_name: str = "orchestrator",
        *,
        docker_client: Any | None = None,
        client_factory: Callable[[str | None], Any] | None = None,
    ) -> None:
        if not isinstance(network_name, str) or not _NAME_PATTERN.fullmatch(network_name):
            raise ValueError("Docker services network name is invalid")
        self.base_url = base_url
        self.network_name = network_name
        self._docker_client = docker_client
        self._client_factory = client_factory or self._create_client

    @classmethod
    def from_settings(cls, settings: Any, *, docker_client: Any | None = None) -> "DockerSwarmAdapter":
        return cls(
            settings.docker_base_url,
            settings.docker_services_network,
            docker_client=docker_client,
        )

    def __repr__(self) -> str:
        return f"DockerSwarmAdapter(base_url={self.base_url!r}, network_name={self.network_name!r})"

    def deploy(self, appid: str, compose: Mapping[str, Any]) -> SwarmDeployment:
        """Create or update every service in a Compose document.

        ``depends_on`` and ``expose`` are Compose orchestration hints, not
        Swarm service properties.  Internal communication happens through the
        application overlay network; only ``ports`` become published endpoint
        specifications.
        """

        namespace = self._namespace(appid)
        services = self._compose_services(compose)
        client = self._client()
        network_name, network_target = self._ensure_network(client, namespace)

        deployments: list[ServiceDeployment] = []
        for compose_name in sorted(services):
            definition = services[compose_name]
            deployments.append(
                self._deploy_one(
                    client,
                    namespace,
                    compose_name,
                    definition,
                    network_target,
                )
            )
        return SwarmDeployment(appid=appid, network_name=network_name, services=tuple(deployments))

    # Explicit spelling for integrations that describe their input as Compose.
    deploy_services = deploy
    deploy_service = deploy

    def deploy_one(self, appid: str, service_name: str, service: Mapping[str, Any]) -> ServiceDeployment:
        """Create or update one service definition without a full Compose document."""

        namespace = self._namespace(appid)
        client = self._client()
        _, network_target = self._ensure_network(client, namespace)
        return self._deploy_one(client, namespace, service_name, service, network_target)

    def deployment_models(self, appid: str, build_id: str, compose: Mapping[str, Any]) -> list[DeploymentService]:
        """Deploy Compose services and return repository-ready domain records."""

        return self.deploy(appid, compose).to_models(build_id)

    def _client(self) -> Any:
        if self._docker_client is None:
            self._docker_client = self._client_factory(self.base_url)
        return self._docker_client

    @staticmethod
    def _create_client(base_url: str | None) -> Any:
        try:
            import docker
        except ImportError as exc:
            raise DockerServiceError("Docker SDK is required for Swarm deployment") from exc
        if base_url:
            return docker.DockerClient(base_url=base_url)
        return docker.from_env()

    def _ensure_network(self, client: Any, appid: str) -> tuple[str, str]:
        name = f"{self.network_name}-{appid}"
        try:
            network = client.networks.get(name)
        except Exception as exc:
            if not self._is_not_found(exc):
                raise DockerServiceError("Unable to inspect the application Docker network") from exc
            try:
                network = client.networks.create(
                    name,
                    driver="overlay",
                    attachable=True,
                    labels={"io.docker-jenkins-orchestrator.appid": appid},
                )
            except Exception as create_exc:
                raise DockerServiceError("Unable to create the application Docker network") from create_exc
        identifier = self._object_id(network) or name
        return name, identifier

    def _deploy_one(
        self,
        client: Any,
        appid: str,
        compose_name: str,
        definition: Mapping[str, Any],
        network_target: str,
    ) -> ServiceDeployment:
        if not isinstance(compose_name, str) or not _NAME_PATTERN.fullmatch(compose_name):
            raise DockerServiceError("Compose service name is invalid")
        if not isinstance(definition, Mapping):
            raise DockerServiceError("Compose service definition is invalid")
        image = definition.get("image")
        if not isinstance(image, str) or not image.strip():
            raise DockerServiceError("Compose service must define an image")

        name = self._service_name(appid, compose_name)
        ports = self._ports(definition.get("ports"))
        kwargs = self._service_kwargs(appid, compose_name, definition, network_target, ports)

        try:
            existing = client.services.get(name)
        except Exception as exc:
            if not self._is_not_found(exc):
                raise DockerServiceError("Unable to inspect Docker service") from exc
            existing = None

        try:
            if existing is None:
                service = client.services.create(image, name=name, **kwargs)
                action: Literal["created", "updated"] = "created"
            else:
                existing.update(image=image, **kwargs)
                service = existing
                action = "updated"
        except Exception as exc:
            raise DockerServiceError("Unable to deploy Docker service") from exc

        service_id = self._object_id(service) or name
        endpoint = self._endpoint(name, ports)
        return ServiceDeployment(
            service_id=service_id,
            service_name=name,
            image=image,
            action=action,
            endpoint=endpoint,
            ports=tuple(ports),
        )

    def _service_kwargs(
        self,
        appid: str,
        compose_name: str,
        definition: Mapping[str, Any],
        network_target: str,
        ports: list[PublishedPort],
    ) -> dict[str, Any]:
        labels = self._labels(appid, compose_name, definition.get("labels"))
        kwargs: dict[str, Any] = {
            "env": self._environment(definition.get("environment")),
            "labels": labels,
            "networks": [network_target],
        }
        command = definition.get("command")
        if command is not None:
            kwargs["command"] = self._command(command)
        working_dir = definition.get("working_dir")
        if working_dir is not None:
            if not isinstance(working_dir, str) or not working_dir.startswith("/"):
                raise DockerServiceError("Compose working directory is invalid")
            kwargs["workdir"] = working_dir
        mounts = self._mounts(definition.get("volumes"))
        if mounts:
            kwargs["mounts"] = mounts
        endpoint_spec = self._endpoint_spec(ports)
        if endpoint_spec is not None:
            kwargs["endpoint_spec"] = endpoint_spec
        mode = self._mode(definition.get("deploy"))
        if mode is not None:
            kwargs["mode"] = mode
        restart_policy = self._restart_policy(definition.get("restart"), definition.get("deploy"))
        if restart_policy is not None:
            kwargs["restart_policy"] = restart_policy
        return kwargs

    @staticmethod
    def _compose_services(compose: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
        if not isinstance(compose, Mapping):
            raise DockerServiceError("Compose document must be a mapping")
        services = compose.get("services")
        if not isinstance(services, Mapping) or not services:
            raise DockerServiceError("Compose document must contain services")
        if not all(isinstance(name, str) and isinstance(value, Mapping) for name, value in services.items()):
            raise DockerServiceError("Compose services must be named mappings")
        return services

    @staticmethod
    def _namespace(appid: str) -> str:
        if not isinstance(appid, str) or not _NAME_PATTERN.fullmatch(appid):
            raise DockerServiceError("Application ID is invalid for Docker service deployment")
        return appid.lower()

    @staticmethod
    def _service_name(appid: str, compose_name: str) -> str:
        prefix = f"{appid}-"
        return compose_name if compose_name.lower().startswith(prefix) else f"{prefix}{compose_name}"

    @staticmethod
    def _labels(appid: str, compose_name: str, raw: Any) -> dict[str, str]:
        labels = {
            "io.docker-jenkins-orchestrator.appid": appid,
            "io.docker-jenkins-orchestrator.compose-service": compose_name,
        }
        if raw is None:
            return labels
        if not isinstance(raw, Mapping):
            raise DockerServiceError("Compose labels must be a mapping")
        for key, value in raw.items():
            if not isinstance(key, str) or not key or value is None:
                raise DockerServiceError("Compose labels are invalid")
            labels[key] = str(value).lower() if isinstance(value, bool) else str(value)
        return labels

    @staticmethod
    def _environment(raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, Mapping):
            if not all(isinstance(key, str) and _ENV_KEY_PATTERN.fullmatch(key) for key in raw):
                raise DockerServiceError("Compose service environment is invalid")
            environment: list[str] = []
            for key in sorted(raw):
                value = raw[key]
                if value is None:
                    raise DockerServiceError("Compose service environment is invalid")
                text = str(value).lower() if isinstance(value, bool) else str(value)
                environment.append(f"{key}={text}")
            return environment
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            environment = []
            for item in raw:
                if not isinstance(item, str) or "=" not in item:
                    raise DockerServiceError("Compose service environment is invalid")
                key, _ = item.split("=", 1)
                if not _ENV_KEY_PATTERN.fullmatch(key):
                    raise DockerServiceError("Compose service environment is invalid")
                environment.append(item)
            return environment
        raise DockerServiceError("Compose service environment is invalid")

    @staticmethod
    def _command(raw: Any) -> str | list[str]:
        if isinstance(raw, str):
            return raw
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and all(isinstance(item, str) for item in raw):
            return list(raw)
        raise DockerServiceError("Compose service command is invalid")

    @classmethod
    def _ports(cls, raw: Any) -> list[PublishedPort]:
        if raw is None:
            return []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise DockerServiceError("Compose service ports must be a list")
        ports: list[PublishedPort] = []
        for item in raw:
            if isinstance(item, str):
                ports.append(cls._short_port(item))
            elif isinstance(item, Mapping):
                ports.append(cls._long_port(item))
            else:
                raise DockerServiceError("Compose service port is invalid")
        return ports

    @classmethod
    def _short_port(cls, raw: str) -> PublishedPort:
        value = raw.strip()
        if not value:
            raise DockerServiceError("Compose service port is invalid")
        protocol = "tcp"
        if "/" in value:
            value, protocol = value.rsplit("/", 1)
        if protocol not in _PORT_PROTOCOLS:
            raise DockerServiceError("Compose service port protocol is invalid")
        parts = value.split(":")
        if len(parts) == 1:
            target, published = cls._port_number(parts[0]), None
        elif len(parts) == 2:
            published, target = cls._port_number(parts[0]), cls._port_number(parts[1])
        else:
            # Swarm endpoint specifications do not support per-service host
            # IP bindings, and port ranges require one service per binding.
            raise DockerServiceError("Compose host-IP and port-range bindings are not supported for Swarm services")
        return PublishedPort(target_port=target, published_port=published, protocol=protocol)

    @classmethod
    def _long_port(cls, raw: Mapping[str, Any]) -> PublishedPort:
        target = cls._port_number(raw.get("target"))
        published_value = raw.get("published")
        published = None if published_value is None else cls._port_number(published_value)
        protocol = raw.get("protocol", "tcp")
        mode = raw.get("mode", "ingress")
        if not isinstance(protocol, str) or not isinstance(mode, str) or protocol not in _PORT_PROTOCOLS or mode not in {
            "ingress",
            "host",
        }:
            raise DockerServiceError("Compose service port is invalid")
        if raw.get("host_ip") not in (None, ""):
            raise DockerServiceError("Compose host-IP bindings are not supported for Swarm services")
        return PublishedPort(target_port=target, published_port=published, protocol=protocol, mode=mode)

    @staticmethod
    def _port_number(value: Any) -> int:
        if isinstance(value, bool):
            raise DockerServiceError("Compose service port is invalid")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise DockerServiceError("Compose service port is invalid") from exc
        if not 1 <= number <= 65535:
            raise DockerServiceError("Compose service port is invalid")
        return number

    @staticmethod
    def _endpoint_spec(ports: list[PublishedPort]) -> dict[str, Any] | None:
        if not ports:
            return None
        return {"Mode": "vip", "Ports": [port.endpoint_spec() for port in ports]}

    @staticmethod
    def _endpoint(service_name: str, ports: list[PublishedPort]) -> str | None:
        for port in ports:
            if port.published_port is not None:
                return f"{service_name}:{port.published_port}"
        return None

    @staticmethod
    def _mounts(raw: Any) -> list[str]:
        if raw is None:
            return []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise DockerServiceError("Compose service volumes must be a list")
        mounts: list[str] = []
        for item in raw:
            if isinstance(item, str) and item:
                mounts.append(item)
                continue
            if not isinstance(item, Mapping):
                raise DockerServiceError("Compose service volume is invalid")
            source, target = item.get("source"), item.get("target")
            if not isinstance(source, str) or not source or not isinstance(target, str) or not target.startswith("/"):
                raise DockerServiceError("Compose service volume is invalid")
            mode = "ro" if item.get("read_only") is True else "rw"
            mounts.append(f"{source}:{target}:{mode}")
        return mounts

    @staticmethod
    def _mode(raw_deploy: Any) -> dict[str, Any] | None:
        if raw_deploy is None:
            return None
        if not isinstance(raw_deploy, Mapping):
            raise DockerServiceError("Compose deploy settings are invalid")
        mode = raw_deploy.get("mode", "replicated")
        if mode == "global":
            return {"global": {}}
        if mode != "replicated":
            raise DockerServiceError("Compose deploy mode is invalid")
        replicas = raw_deploy.get("replicas", 1)
        if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 0:
            raise DockerServiceError("Compose service replica count is invalid")
        return {"replicated": {"Replicas": replicas}}

    @staticmethod
    def _restart_policy(restart: Any, raw_deploy: Any) -> dict[str, str] | None:
        deploy_policy = raw_deploy.get("restart_policy") if isinstance(raw_deploy, Mapping) else None
        if deploy_policy is not None:
            if not isinstance(deploy_policy, Mapping):
                raise DockerServiceError("Compose restart policy is invalid")
            condition = deploy_policy.get("condition", "any")
            if condition not in {"none", "on-failure", "any"}:
                raise DockerServiceError("Compose restart policy is invalid")
            return {"Condition": condition}
        if restart is None:
            return None
        mapping = {
            "no": "none",
            "always": "any",
            "unless-stopped": "any",
            "on-failure": "on-failure",
        }
        if not isinstance(restart, str) or restart not in mapping:
            raise DockerServiceError("Compose restart policy is invalid")
        return {"Condition": mapping[restart]}

    @staticmethod
    def _object_id(value: Any) -> str | None:
        identifier = getattr(value, "id", None)
        if isinstance(identifier, str) and identifier:
            return identifier
        attributes = getattr(value, "attrs", None)
        if isinstance(attributes, Mapping):
            for key in ("ID", "Id"):
                identifier = attributes.get(key)
                if isinstance(identifier, str) and identifier:
                    return identifier
        if isinstance(value, Mapping):
            for key in ("ID", "Id", "id"):
                identifier = value.get(key)
                if isinstance(identifier, str) and identifier:
                    return identifier
        return None

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        return isinstance(exc, KeyError) or getattr(exc, "status_code", None) == 404 or exc.__class__.__name__ == "NotFound"


# A descriptive alias for callers using the existing adapter terminology.
DockerServicesAdapter = DockerSwarmAdapter
