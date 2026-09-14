"""Docker Swarm deployment adapter for rendered Compose service mappings."""

from __future__ import annotations

import re
from hashlib import sha256
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal

from orchestrator.adapters import AdapterError
from orchestrator.models import DeploymentService


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PORT_PROTOCOLS = {"tcp", "udp", "sctp"}
_DURATION_PART = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ns|us|ms|s|m|h)")
_SUPPORTED_SERVICE_FIELDS = {
    "image",
    "command",
    "environment",
    "ports",
    "volumes",
    "shm_size",
    "working_dir",
    "labels",
    "restart",
    "healthcheck",
    "deploy",
    # Compose orchestration metadata is intentionally handled outside a
    # Swarm service specification.  The application overlay network provides
    # service discovery and Docker's scheduler provides restart behavior.
    "depends_on",
    "expose",
}
_REJECTED_SERVICE_FIELDS = {
    "build",
    "container_name",
    "configs",
    "devices",
    "dns",
    "dns_opt",
    "dns_search",
    "entrypoint",
    "extra_hosts",
    "gpus",
    "init",
    "ipc",
    "links",
    "logging",
    "network_mode",
    "networks",
    "pid",
    "privileged",
    "profiles",
    "read_only",
    "runtime",
    "secrets",
    "stop_grace_period",
    "stop_signal",
    "sysctls",
    "tmpfs",
    "tty",
    "ulimits",
    "user",
    "working_dir_host",
}
_SUPPORTED_DEPLOY_FIELDS = {"mode", "replicas", "restart_policy"}
_DEPENDENCY_CONDITIONS = {"service_started", "service_healthy"}
_SUPPORTED_TOP_LEVEL_FIELDS = {"version", "name", "services", "volumes"}
_MAX_DOCKER_NAME_LENGTH = 63


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
        specifications.  ``env_file`` is deliberately rejected because a
        Jenkins result artifact contains JSON, not the referenced file.  The
        caller must resolve those values into ``environment`` before handing
        the document to this adapter.
        """

        namespace = self._namespace(appid)
        # Complete every deterministic validation before connecting to Docker.
        # In particular, image presence, service names, and dependency cycles
        # used to be checked while the deployment loop was already under way.
        self.validate_compose(compose)
        services = self._compose_services(compose)
        volume_sources = self._volume_sources(compose)
        client = self._client()
        network_name, network_target = self._ensure_network(client, namespace)

        deployments: list[ServiceDeployment] = []
        for compose_name in self._deployment_order(services):
            definition = services[compose_name]
            deployments.append(
                self._deploy_one(
                    client,
                    namespace,
                    compose_name,
                    definition,
                    network_target,
                    volume_sources,
                )
            )
        self._cleanup_stale_services(client, namespace, {item.service_name for item in deployments})
        return SwarmDeployment(appid=appid, network_name=network_name, services=tuple(deployments))

    # Explicit spelling for integrations that describe their input as Compose.
    deploy_services = deploy
    deploy_service = deploy

    def deploy_one(self, appid: str, service_name: str, service: Mapping[str, Any]) -> ServiceDeployment:
        """Create or update one service definition without a full Compose document."""

        namespace = self._namespace(appid)
        if not isinstance(service, Mapping):
            raise DockerServiceError("Compose service definition is invalid")
        self._validate_compose_features({service_name: service})
        self._validate_service_name(service_name)
        self._validate_service_image(service_name, service)
        client = self._client()
        _, network_target = self._ensure_network(client, namespace)
        return self._deploy_one(client, namespace, service_name, service, network_target, {})

    def deployment_models(self, appid: str, build_id: str, compose: Mapping[str, Any]) -> list[DeploymentService]:
        """Deploy Compose services and return repository-ready domain records."""

        return self.deploy(appid, compose).to_models(build_id)

    @classmethod
    def validate_compose(cls, compose: Any, *, require_images: bool = True) -> None:
        """Validate a Compose artifact before opening a Docker connection."""

        cls._validate_top_level(compose)
        services = cls._compose_services(compose)
        cls._validate_compose_features(services)
        cls._deployment_order(services)
        for name, definition in services.items():
            cls._validate_service_name(name)
            if require_images:
                cls._validate_service_image(name, definition)

    @staticmethod
    def _validate_service_name(name: Any) -> None:
        if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
            raise DockerServiceError("Compose service name is invalid")

    @staticmethod
    def _validate_service_image(name: str, definition: Mapping[str, Any]) -> None:
        image = definition.get("image")
        if not isinstance(image, str) or not image.strip():
            raise DockerServiceError(f"Compose service {name} must define an image")

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
        name = self._network_name(appid)
        try:
            network = client.networks.get(name)
        except Exception as exc:
            if not self._is_not_found(exc):
                raise DockerServiceError(
                    f"Unable to inspect Docker network {name}: {self._safe_error_detail(exc)}"
                ) from exc
            try:
                network = client.networks.create(
                    name,
                    driver="overlay",
                    attachable=True,
                    labels={"io.docker-jenkins-orchestrator.appid": appid},
                )
            except Exception as create_exc:
                raise DockerServiceError(
                    f"Unable to create Docker network {name}: {self._safe_error_detail(create_exc)}"
                ) from create_exc
        identifier = self._object_id(network) or name
        return name, identifier

    def _cleanup_stale_services(self, client: Any, appid: str, desired_names: set[str]) -> None:
        """Remove services from an earlier Compose revision that disappeared.

        Reconciliation happens only after all desired services have been
        created or updated, so a failed rollout does not remove the previous
        working service set.  ``deploy_one`` intentionally skips this pass as
        it represents a partial update.
        """

        services_api = getattr(client, "services", None)
        list_services = getattr(services_api, "list", None)
        if not callable(list_services):
            # Keep small injected test doubles and older SDK wrappers usable;
            # production Docker SDK service collections always provide list().
            return
        try:
            existing_services = list_services(
                filters={"label": f"io.docker-jenkins-orchestrator.appid={appid}"}
            )
        except Exception as exc:
            raise DockerServiceError(
                f"Unable to list Docker services for app {appid}: {self._safe_error_detail(exc)}"
            ) from exc
        for service in existing_services:
            name = self._service_name_from_object(service)
            if not name or name in desired_names:
                continue
            try:
                remove = getattr(service, "remove", None)
                if not callable(remove):
                    raise RuntimeError("service remove operation is unavailable")
                remove()
            except Exception as exc:
                raise DockerServiceError(
                    f"Unable to remove stale Docker service {name}: {self._safe_error_detail(exc)}"
                ) from exc

    @classmethod
    def _service_name_from_object(cls, service: Any) -> str | None:
        name = getattr(service, "name", None)
        if isinstance(name, str) and name:
            return name
        attrs = getattr(service, "attrs", None)
        if isinstance(attrs, Mapping):
            name = attrs.get("Spec", {}).get("Name") if isinstance(attrs.get("Spec"), Mapping) else None
            if isinstance(name, str) and name:
                return name
            name = attrs.get("Name")
            if isinstance(name, str) and name:
                return name
        if isinstance(service, Mapping):
            for key in ("Name", "name"):
                name = service.get(key)
                if isinstance(name, str) and name:
                    return name
        return None

    @classmethod
    def _volume_sources(cls, compose: Mapping[str, Any]) -> dict[str, str]:
        raw_volumes = compose.get("volumes")
        if not isinstance(raw_volumes, Mapping):
            return {}
        sources: dict[str, str] = {}
        for alias, definition in raw_volumes.items():
            if not isinstance(alias, str) or not alias:
                continue
            if not isinstance(definition, Mapping):
                sources[alias] = alias
                continue
            configured_name = definition.get("name")
            if isinstance(configured_name, str) and configured_name.strip():
                sources[alias] = configured_name.strip()
            else:
                sources[alias] = alias
        return sources

    @classmethod
    def _deployment_order(cls, services: Mapping[str, Mapping[str, Any]]) -> list[str]:
        """Return a dependency-first order for Compose service creation.

        Swarm does not implement Compose's ``depends_on`` readiness gate, but
        creating dependencies first gives applications that retry connections
        a deterministic startup order.  Health checks remain attached to the
        service and are used by the application itself; this adapter does not
        block a rollout waiting for a task to become healthy.
        """

        dependencies: dict[str, set[str]] = {}
        for name, definition in services.items():
            raw = definition.get("depends_on")
            if raw is None:
                dependencies[name] = set()
            elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                if not all(isinstance(item, str) and item for item in raw):
                    raise DockerServiceError(f"Compose service {name} has an invalid dependency name")
                dependencies[name] = set(raw)
            elif isinstance(raw, Mapping):
                if not all(isinstance(item, str) and item for item in raw):
                    raise DockerServiceError(f"Compose service {name} has an invalid dependency name")
                dependencies[name] = set(raw)
            else:
                raise DockerServiceError(f"Compose depends_on for {name} is invalid")

        ordered: list[str] = []
        remaining = {name: set(items) for name, items in dependencies.items()}
        while remaining:
            ready = sorted(name for name, items in remaining.items() if not items)
            if not ready:
                raise DockerServiceError("Compose depends_on contains a cycle")
            ordered.extend(ready)
            for name in ready:
                remaining.pop(name)
            for items in remaining.values():
                items.difference_update(ready)
        return ordered

    def _deploy_one(
        self,
        client: Any,
        appid: str,
        compose_name: str,
        definition: Mapping[str, Any],
        network_target: str,
        volume_sources: Mapping[str, str],
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
        kwargs = self._service_kwargs(appid, compose_name, definition, network_target, ports, volume_sources)

        try:
            existing = client.services.get(name)
        except Exception as exc:
            if not self._is_not_found(exc):
                raise DockerServiceError(
                    f"Unable to inspect Docker service {name}: {self._safe_error_detail(exc)}"
                ) from exc
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
            raise DockerServiceError(
                f"Unable to {'create' if existing is None else 'update'} Docker service "
                f"{name}: {self._safe_error_detail(exc)}"
            ) from exc

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
        volume_sources: Mapping[str, str],
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
        mounts = self._mounts(definition.get("volumes"), volume_sources)
        shm_size = definition.get("shm_size")
        if shm_size is not None:
            mounts.append(self._shm_mount(shm_size))
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
        resources = self._resources(definition.get("deploy"))
        if resources is not None:
            kwargs["resources"] = resources
        healthcheck = self._healthcheck(definition.get("healthcheck"))
        if healthcheck is not None:
            kwargs["healthcheck"] = healthcheck
        return kwargs

    @classmethod
    def _validate_compose_features(cls, services: Mapping[str, Mapping[str, Any]]) -> None:
        """Reject Compose inputs whose values cannot be represented safely.

        Compose ``env_file`` entries are paths resolved by Compose itself.  A
        Jenkins artifact has no filesystem context for those paths, so
        silently dropping them would create a service with incomplete
        credentials and connection settings.  Reject the whole document
        before creating the application network or any service.
        """

        for name, definition in services.items():
            # Run all pure input conversions before deploy() creates the
            # application network. This keeps malformed artifacts atomic:
            # validation errors must not leave infrastructure behind.
            cls._environment(definition.get("environment"))
            cls._labels("validation", name, definition.get("labels"))
            cls._ports(definition.get("ports"))
            command = definition.get("command")
            if command is not None:
                cls._command(command)
            working_dir = definition.get("working_dir")
            if working_dir is not None and (not isinstance(working_dir, str) or not working_dir.startswith("/")):
                raise DockerServiceError("Compose working directory is invalid")
            cls._mounts(definition.get("volumes"))
            if definition.get("shm_size") is not None:
                cls._shm_mount(definition["shm_size"])
            cls._healthcheck(definition.get("healthcheck"))
            cls._mode(definition.get("deploy"))
            cls._restart_policy(definition.get("restart"), definition.get("deploy"))
            cls._resources(definition.get("deploy"))
            if "env_file" in definition:
                raise DockerServiceError(
                    f"Compose service {name} uses env_file; resolve it into environment before deployment"
                )
            rejected = sorted(
                key
                for key in definition
                if key in cls._rejected_service_fields() or (key not in _SUPPORTED_SERVICE_FIELDS and not key.startswith("x-"))
            )
            if rejected:
                raise DockerServiceError(
                    f"Compose service {name} uses unsupported fields: {', '.join(rejected)}"
                )
            deploy = definition.get("deploy")
            if deploy is not None:
                if not isinstance(deploy, Mapping):
                    raise DockerServiceError(f"Compose deploy settings for {name} are invalid")
                unsupported_deploy = sorted(set(deploy) - (_SUPPORTED_DEPLOY_FIELDS | {"resources"}))
                if unsupported_deploy:
                    raise DockerServiceError(
                        f"Compose deploy settings for {name} use unsupported fields: {', '.join(unsupported_deploy)}"
                    )
                restart_policy = deploy.get("restart_policy")
                if isinstance(restart_policy, Mapping):
                    unsupported_policy = sorted(set(restart_policy) - {"condition"})
                    if unsupported_policy:
                        raise DockerServiceError(
                            f"Compose restart policy for {name} uses unsupported fields: {', '.join(unsupported_policy)}"
                        )
                resources = deploy.get("resources")
                if resources is not None:
                    cls._validate_resources(name, resources)

            depends_on = definition.get("depends_on")
            if depends_on is not None:
                if isinstance(depends_on, Sequence) and not isinstance(depends_on, (str, bytes)):
                    if not all(isinstance(item, str) and item for item in depends_on):
                        raise DockerServiceError(f"Compose service {name} has an invalid dependency name")
                    dependencies = {item: {} for item in depends_on}
                elif isinstance(depends_on, Mapping):
                    dependencies = depends_on
                else:
                    raise DockerServiceError(f"Compose depends_on for {name} is invalid")
                for dependency, config in dependencies.items():
                    if not isinstance(dependency, str) or not dependency:
                        raise DockerServiceError(f"Compose service {name} has an invalid dependency name")
                    if dependency not in services:
                        raise DockerServiceError(f"Compose service {name} depends on unknown service {dependency}")
                    if config in (None, {}):
                        continue
                    if not isinstance(config, Mapping):
                        raise DockerServiceError(f"Compose dependency {name}->{dependency} is invalid")
                    unsupported_config = sorted(set(config) - {"condition"})
                    if unsupported_config:
                        raise DockerServiceError(
                            f"Compose dependency {name}->{dependency} uses unsupported fields: {', '.join(unsupported_config)}"
                        )
                    condition = config.get("condition", "service_started")
                    if condition not in _DEPENDENCY_CONDITIONS:
                        raise DockerServiceError(
                            f"Compose dependency {name}->{dependency} uses unsupported condition {condition}"
                        )
                    if condition == "service_healthy" and not services[dependency].get("healthcheck"):
                        raise DockerServiceError(
                            f"Compose dependency {name}->{dependency} requires service_healthy but has no healthcheck"
                        )

    @staticmethod
    def _rejected_service_fields() -> set[str]:
        # Keep this as a method so downstream integrations can subclass the
        # adapter and explicitly opt into a field after adding a safe mapping.
        return _REJECTED_SERVICE_FIELDS

    @classmethod
    def _validate_resources(cls, service_name: str, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            raise DockerServiceError(f"Compose resources for {service_name} are invalid")
        unsupported = sorted(set(raw) - {"limits", "reservations"})
        if unsupported:
            raise DockerServiceError(
                f"Compose resources for {service_name} use unsupported fields: {', '.join(unsupported)}"
            )
        for section in ("limits", "reservations"):
            values = raw.get(section)
            if values is None:
                continue
            if not isinstance(values, Mapping):
                raise DockerServiceError(f"Compose {section} resources for {service_name} are invalid")
            unsupported_values = sorted(set(values) - {"cpus", "memory"})
            if unsupported_values:
                raise DockerServiceError(
                    f"Compose {section} resources for {service_name} use unsupported fields: {', '.join(unsupported_values)}"
                )
            for key, value in values.items():
                if key == "cpus":
                    cls._cpu_nano(value)
                else:
                    cls._memory_bytes(value)

    @classmethod
    def _resources(cls, raw_deploy: Any) -> dict[str, Any] | None:
        if not isinstance(raw_deploy, Mapping) or raw_deploy.get("resources") is None:
            return None
        raw = raw_deploy["resources"]
        cls._validate_resources("service", raw)
        result: dict[str, Any] = {}
        for source, target in (("limits", "Limits"), ("reservations", "Reservations")):
            values = raw.get(source)
            if not values:
                continue
            mapped: dict[str, int] = {}
            if "cpus" in values:
                mapped["NanoCPUs"] = cls._cpu_nano(values["cpus"])
            if "memory" in values:
                mapped["MemoryBytes"] = cls._memory_bytes(values["memory"])
            if mapped:
                result[target] = mapped
        return result or None

    @staticmethod
    def _cpu_nano(raw: Any) -> int:
        if isinstance(raw, bool):
            raise DockerServiceError("Compose CPU resource is invalid")
        try:
            value = Decimal(str(raw))
            nanos = int(value * Decimal(1_000_000_000))
        except (InvalidOperation, TypeError, ValueError):
            raise DockerServiceError("Compose CPU resource is invalid") from None
        if value <= 0 or nanos <= 0:
            raise DockerServiceError("Compose CPU resource is invalid")
        return nanos

    @staticmethod
    def _memory_bytes(raw: Any) -> int:
        if isinstance(raw, bool):
            raise DockerServiceError("Compose memory resource is invalid")
        try:
            from docker.utils import parse_bytes

            value = parse_bytes(raw) if isinstance(raw, str) else int(raw)
        except (ImportError, TypeError, ValueError):
            raise DockerServiceError("Compose memory resource is invalid") from None
        if value <= 0:
            raise DockerServiceError("Compose memory resource is invalid")
        return value

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
    def _validate_top_level(compose: Any) -> None:
        if not isinstance(compose, Mapping):
            raise DockerServiceError("Compose document must be a mapping")
        unsupported = sorted(
            key for key in compose if key not in _SUPPORTED_TOP_LEVEL_FIELDS and not str(key).startswith("x-")
        )
        if unsupported:
            raise DockerServiceError(
                f"Compose document uses unsupported top-level fields: {', '.join(map(str, unsupported))}"
            )
        volumes = compose.get("volumes")
        if volumes is None:
            return
        if not isinstance(volumes, Mapping):
            raise DockerServiceError("Compose top-level volumes must be a mapping")
        for name, definition in volumes.items():
            if not isinstance(name, str) or not name:
                raise DockerServiceError("Compose volume name is invalid")
            if definition in (None, {}):
                continue
            if not isinstance(definition, Mapping):
                raise DockerServiceError(f"Compose volume {name} is invalid")
            unsupported_volume = sorted(set(definition) - {"name", "external"})
            if unsupported_volume:
                raise DockerServiceError(
                    f"Compose volume {name} uses unsupported fields: {', '.join(unsupported_volume)}"
                )
            if "name" in definition and (not isinstance(definition["name"], str) or not definition["name"].strip()):
                raise DockerServiceError(f"Compose volume {name} has an invalid name")
            if "external" in definition and not isinstance(definition["external"], bool):
                raise DockerServiceError(f"Compose volume {name} has an invalid external flag")

    @staticmethod
    def _namespace(appid: str) -> str:
        if not isinstance(appid, str) or not _NAME_PATTERN.fullmatch(appid):
            raise DockerServiceError("Application ID is invalid for Docker service deployment")
        return appid.lower()

    def _network_name(self, appid: str) -> str:
        raw = f"{self.network_name}-{appid}"
        if len(raw) <= _MAX_DOCKER_NAME_LENGTH:
            return raw
        digest = sha256(appid.encode("utf-8")).hexdigest()[:10]
        prefix_budget = _MAX_DOCKER_NAME_LENGTH - len(self.network_name) - len(digest) - 2
        prefix = appid[: max(1, prefix_budget)]
        return f"{self.network_name}-{prefix}-{digest}"[:_MAX_DOCKER_NAME_LENGTH]

    @staticmethod
    def _service_name(appid: str, compose_name: str) -> str:
        prefix = f"{appid}-"
        raw = compose_name if compose_name.lower().startswith(prefix.lower()) else f"{prefix}{compose_name}"
        if len(raw) <= _MAX_DOCKER_NAME_LENGTH:
            return raw
        digest = sha256(f"{appid}:{compose_name}".encode("utf-8")).hexdigest()[:10]
        suffix = f"-{digest}-{compose_name}"
        prefix_budget = _MAX_DOCKER_NAME_LENGTH - len(suffix)
        if prefix_budget > 0:
            return f"{appid[:prefix_budget]}{suffix}"
        compose_budget = max(1, _MAX_DOCKER_NAME_LENGTH - len(digest) - 2)
        return f"{digest}-{compose_name[:compose_budget]}"[:_MAX_DOCKER_NAME_LENGTH]

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
    def _mounts(raw: Any, volume_sources: Mapping[str, str] | None = None) -> list[str]:
        if raw is None:
            return []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise DockerServiceError("Compose service volumes must be a list")
        mounts: list[str] = []
        volume_sources = volume_sources or {}
        for item in raw:
            if isinstance(item, str) and item:
                parts = item.split(":")
                if len(parts) in {2, 3} and not parts[0].startswith("/") and parts[0] in volume_sources:
                    parts[0] = volume_sources[parts[0]]
                    item = ":".join(parts)
                elif len(parts) in {2, 3} and parts[0].startswith((".", "~")):
                    raise DockerServiceError("Compose relative bind volume paths are not supported for Swarm services")
                mounts.append(item)
                continue
            if not isinstance(item, Mapping):
                raise DockerServiceError("Compose service volume is invalid")
            source, target = item.get("source"), item.get("target")
            if not isinstance(source, str) or not source or not isinstance(target, str) or not target.startswith("/"):
                raise DockerServiceError("Compose service volume is invalid")
            mount_type = item.get("type", "volume")
            if mount_type not in {"volume", "bind"}:
                raise DockerServiceError("Compose service volume type is invalid")
            if mount_type == "bind" and not source.startswith("/"):
                raise DockerServiceError("Compose relative bind volume paths are not supported for Swarm services")
            if item.get("read_only") not in (None, True, False):
                raise DockerServiceError("Compose service volume read_only flag is invalid")
            mode = "ro" if item.get("read_only") is True else "rw"
            if mount_type == "volume" and source in volume_sources:
                source = volume_sources[source]
            mounts.append(f"{source}:{target}:{mode}")
        return mounts

    @staticmethod
    def _shm_mount(raw: Any) -> dict[str, Any]:
        """Represent Compose ``shm_size`` using a Swarm tmpfs mount.

        Swarm's service API does not expose the standalone Compose
        ``shm_size`` field.  A tmpfs mounted at ``/dev/shm`` has the same
        container-visible semantics and is accepted by Docker's ContainerSpec.
        """

        try:
            from docker.utils import parse_bytes

            size = parse_bytes(raw) if isinstance(raw, str) else int(raw)
        except (ImportError, TypeError, ValueError):
            raise DockerServiceError("Compose shm_size is invalid") from None
        if size <= 0:
            raise DockerServiceError("Compose shm_size is invalid")
        return {
            "Type": "tmpfs",
            "Source": "",
            "Target": "/dev/shm",
            "ReadOnly": False,
            "TmpfsOptions": {"SizeBytes": size},
        }

    @staticmethod
    def _healthcheck(raw: Any) -> dict[str, Any] | None:
        """Normalize Compose healthcheck values to Docker API field names."""

        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise DockerServiceError("Compose healthcheck is invalid")
        if raw.get("disable") is True:
            return {"Test": ["NONE"]}
        if raw.get("disable") not in (None, False):
            raise DockerServiceError("Compose healthcheck disable flag is invalid")

        test = raw.get("test")
        if test is None:
            # ``disable: false`` means inherit the image healthcheck.  No
            # service-level Healthcheck object is needed in that case.
            return None
        if isinstance(test, str):
            normalized_test: list[str] = ["CMD-SHELL", test]
        elif isinstance(test, Sequence) and not isinstance(test, (str, bytes)) and all(
            isinstance(item, str) for item in test
        ):
            normalized_test = list(test)
        else:
            raise DockerServiceError("Compose healthcheck test is invalid")
        if not normalized_test:
            raise DockerServiceError("Compose healthcheck test is invalid")

        result: dict[str, Any] = {"Test": normalized_test}
        for source, target in (
            ("interval", "Interval"),
            ("timeout", "Timeout"),
            ("start_period", "StartPeriod"),
        ):
            if source in raw and raw[source] is not None:
                result[target] = DockerSwarmAdapter._duration_ns(raw[source])
        if "retries" in raw and raw["retries"] is not None:
            retries = raw["retries"]
            if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
                raise DockerServiceError("Compose healthcheck retries are invalid")
            result["Retries"] = retries
        unsupported = set(raw) - {"test", "disable", "interval", "timeout", "start_period", "retries"}
        if unsupported:
            raise DockerServiceError("Compose healthcheck contains unsupported fields")
        return result

    @staticmethod
    def _duration_ns(raw: Any) -> int:
        if isinstance(raw, bool):
            raise DockerServiceError("Compose healthcheck duration is invalid")
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str):
            text = raw.strip()
            if not text:
                raise DockerServiceError("Compose healthcheck duration is invalid")
            position = 0
            total = Decimal(0)
            multipliers = {"ns": Decimal(1), "us": Decimal(1_000), "ms": Decimal(1_000_000), "s": Decimal(1_000_000_000), "m": Decimal(60_000_000_000), "h": Decimal(3_600_000_000_000)}
            try:
                while position < len(text):
                    match = _DURATION_PART.match(text, position)
                    if match is None:
                        raise ValueError
                    total += Decimal(match.group("value")) * multipliers[match.group("unit")]
                    position = match.end()
                value = int(total)
            except (InvalidOperation, ValueError):
                raise DockerServiceError("Compose healthcheck duration is invalid") from None
        else:
            raise DockerServiceError("Compose healthcheck duration is invalid")
        if value < 0:
            raise DockerServiceError("Compose healthcheck duration is invalid")
        return value

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

    @staticmethod
    def _safe_error_detail(exc: Exception) -> str:
        """Return a bounded Docker error explanation without credentials.

        Docker SDK API errors expose a server-provided ``explanation``.  Keep
        that useful context in the build alert, but strip URL credentials and
        cap the length so a low-level response cannot become an event-log dump.
        """

        detail = getattr(exc, "explanation", None) or str(exc) or exc.__class__.__name__
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        detail = str(detail).replace("\r", " ").replace("\n", " ").strip()
        detail = re.sub(r"(https?://)([^/@\s]+):([^/@\s]+)@", r"\1***:***@", detail)
        return detail[:300] or exc.__class__.__name__


# A descriptive alias for callers using the existing adapter terminology.
DockerServicesAdapter = DockerSwarmAdapter
