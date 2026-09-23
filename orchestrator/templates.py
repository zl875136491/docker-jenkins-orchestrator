"""Static component templates and deterministic Docker Compose rendering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
import re
from typing import Any

import yaml


class TemplateError(ValueError):
    """Raised when a template catalog or compose request is invalid."""


_APPID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_DEPENDENCY_CONDITIONS = {
    "service_started",
    "service_healthy",
    "service_completed_successfully",
}
_SCALAR_TYPES = (str, int, float, bool)


class TemplateCatalog:
    """Load static service definitions and compose selected components.

    ``compose(components, dependencies)`` intentionally keeps the original
    call shape.  Additional keyword arguments make a generated document safe
    to associate with a single user application without requiring callers to
    mutate the catalog or the returned document.
    """

    def __init__(self, path: Path) -> None:
        try:
            with path.open(encoding="utf-8") as stream:
                document = yaml.safe_load(stream) or {}
        except OSError as exc:
            raise TemplateError(f"Unable to read template catalog: {path}") from exc
        except yaml.YAMLError as exc:
            raise TemplateError(f"Invalid template catalog YAML: {exc}") from exc

        if not isinstance(document, Mapping):
            raise TemplateError("Template catalog must be a mapping")
        components = document.get("components")
        if not isinstance(components, Mapping) or not components:
            raise TemplateError("Template catalog must contain a non-empty components mapping")

        self.components: dict[str, dict[str, Any]] = {}
        for name, component in components.items():
            if not isinstance(name, str) or not name:
                raise TemplateError("Component names must be non-empty strings")
            self.components[name] = self._validate_component(name, component)
        aliases = document.get("aliases", {})
        if not isinstance(aliases, Mapping):
            raise TemplateError("Template catalog aliases must be a mapping")
        self.aliases: dict[str, str] = {}
        for alias, target in aliases.items():
            if not isinstance(alias, str) or not isinstance(target, str) or target not in self.components:
                raise TemplateError("Each template alias must reference a known component")
            self.aliases[alias.lower()] = target

    def names(self) -> list[str]:
        return sorted(self.components)

    def validate_component(self, name: str, component: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and normalize one editable technology-stack definition."""

        if not isinstance(name, str) or not _APPID_PATTERN.fullmatch(name):
            raise TemplateError("Technology stack id must contain only letters, numbers, underscores, and hyphens")
        return self._validate_component(name, component)

    def upsert_component(self, name: str, component: Mapping[str, Any]) -> dict[str, Any]:
        normalized = self.validate_component(name, component)
        self.components[name] = normalized
        return deepcopy(normalized)

    def remove_component(self, name: str) -> bool:
        return self.components.pop(name, None) is not None

    def load_components(self, components: Mapping[str, Mapping[str, Any]]) -> None:
        self.components = {}
        for name, component in components.items():
            self.upsert_component(name, component)

    def component_yaml(self, name: str) -> str:
        if name not in self.components:
            raise TemplateError(f"Unknown component: {name}")
        return yaml.safe_dump(
            deepcopy(self.components[name]),
            allow_unicode=False,
            default_flow_style=False,
            sort_keys=False,
        )

    def compose(
        self,
        components: list[str],
        dependencies: Mapping[str, Sequence[str] | Mapping[str, Any]] | None = None,
        *,
        appid: str | None = None,
        environment: Mapping[str, Any] | None = None,
        service_environment: Mapping[str, Mapping[str, Any]] | None = None,
        service_environments: Mapping[str, Mapping[str, Any]] | None = None,
        dependency_conditions: bool = False,
    ) -> dict[str, Any]:
        """Return a deterministic Compose document for selected components.

        ``environment`` is injected into every selected service.  Per-service
        values supplied through ``service_environment`` take precedence.  The
        plural spelling is accepted as an alias for integrations that use it.

        Dependency values normally remain lists for compatibility with the
        original API.  Pass ``dependency_conditions=True`` to produce long
        Compose ``depends_on`` syntax, using ``service_healthy`` where a
        dependency defines a healthcheck.  A dependency mapping can also set
        explicit conditions, for example ``{"api": {"postgresql":
        "service_healthy"}}``.
        """

        if not isinstance(dependency_conditions, bool):
            raise TemplateError("dependency_conditions must be a boolean")
        selected = self._validate_selection(components)
        namespace = self._validate_appid(appid)
        global_environment = self._normalize_environment(environment, "environment")
        overrides = self._normalize_service_environments(
            selected,
            namespace,
            service_environment,
            service_environments,
        )
        normalized_dependencies = self._normalize_dependencies(selected, dependencies)
        self._assert_acyclic(selected, normalized_dependencies)

        service_names = {name: self._service_name(name, namespace) for name in selected}
        services: dict[str, Any] = {}
        volumes: dict[str, Any] = {}

        for name in selected:
            component = self.components[name]
            service_name = service_names[name]
            service: dict[str, Any] = {"image": component["images"][0]}

            ports = component["ports"]
            service["expose"] = [str(port) for port in ports]
            if component["publish_ports"]:
                service["ports"] = [f"{port}:{port}" for port in ports]

            if component["working_dir"]:
                service["working_dir"] = component["working_dir"]
            if component["command"] is not None:
                service["command"] = deepcopy(component["command"])
            if component["restart"]:
                service["restart"] = component["restart"]

            rendered_volumes, declared_volumes = self._render_volumes(component, service_name)
            if rendered_volumes:
                service["volumes"] = rendered_volumes
                volumes.update(declared_volumes)

            service_environment_values = self._render_environment_values(
                component["environment"],
                appid=namespace or "",
                component=name,
                host=service_name,
                port=component["ports"][0],
            )
            service_environment_values.update(
                self._internal_connection_environment(namespace, normalized_dependencies[name], service_names)
            )
            service_environment_values.update(global_environment)
            service_environment_values.update(overrides.get(name, {}))
            if service_environment_values:
                service["environment"] = self._ordered_mapping(service_environment_values)

            rendered_dependencies = self._render_dependencies(
                name,
                normalized_dependencies[name],
                service_names,
                dependency_conditions,
            )
            if rendered_dependencies:
                service["depends_on"] = rendered_dependencies

            if component["healthcheck"]:
                service["healthcheck"] = deepcopy(component["healthcheck"])

            if namespace:
                service["labels"] = {
                    "io.docker-jenkins-orchestrator.appid": namespace,
                    "io.docker-jenkins-orchestrator.component": name,
                }
            services[service_name] = service

        document: dict[str, Any] = {"version": "3.9", "services": services}
        if namespace:
            # Compose uses the project name to isolate its default network.
            document = {"name": namespace, **document}
        if volumes:
            document["volumes"] = self._ordered_mapping(volumes)

        self.validate_compose_document(document)
        return document

    def compose_yaml(
        self,
        components: list[str],
        dependencies: Mapping[str, Sequence[str] | Mapping[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        """Compose and serialize a YAML document, validating the round trip."""

        return self.render_compose(self.compose(components, dependencies, **kwargs))

    def render_compose(self, document: Mapping[str, Any]) -> str:
        """Serialize a Compose document and ensure it survives YAML parsing."""

        self.validate_compose_document(document)
        rendered = yaml.safe_dump(
            deepcopy(dict(document)),
            allow_unicode=False,
            default_flow_style=False,
            sort_keys=False,
        )
        parsed = self.parse_compose_yaml(rendered)
        if parsed != document:
            raise TemplateError("Rendered compose YAML did not round-trip cleanly")
        return rendered

    def render_yaml(
        self,
        components: list[str],
        dependencies: Mapping[str, Sequence[str] | Mapping[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        """Alias for :meth:`compose_yaml` for callers that request YAML directly."""

        return self.compose_yaml(components, dependencies, **kwargs)

    def parse_compose_yaml(self, rendered: str) -> dict[str, Any]:
        """Parse and validate rendered Compose YAML without executing it."""

        try:
            document = yaml.safe_load(rendered)
        except yaml.YAMLError as exc:
            raise TemplateError(f"Invalid compose YAML: {exc}") from exc
        self.validate_compose_document(document)
        return document

    def validate_compose_document(self, document: Any) -> None:
        """Validate the parts of the Compose schema produced by this renderer."""

        if not isinstance(document, Mapping):
            raise TemplateError("Compose document must be a mapping")
        if document.get("version") != "3.9":
            raise TemplateError('Compose document version must be "3.9"')
        services = document.get("services")
        if not isinstance(services, Mapping) or not services:
            raise TemplateError("Compose document must contain services")

        service_names = set(services)
        for name, service in services.items():
            if not isinstance(name, str) or not isinstance(service, Mapping):
                raise TemplateError("Compose services must be named mappings")
            image = service.get("image")
            if not isinstance(image, str) or not image:
                raise TemplateError(f"Service {name} must define an image")
            depends_on = service.get("depends_on")
            if depends_on is None:
                continue
            if isinstance(depends_on, list):
                dependency_names = depends_on
                if not all(isinstance(item, str) and item in service_names for item in dependency_names):
                    raise TemplateError(f"Service {name} has an unknown dependency")
                if len(set(dependency_names)) != len(dependency_names):
                    raise TemplateError(f"Service {name} has duplicate dependencies")
                continue
            if not isinstance(depends_on, Mapping):
                raise TemplateError(f"Service {name} has invalid depends_on")
            for dependency, config in depends_on.items():
                if dependency not in service_names or not isinstance(config, Mapping):
                    raise TemplateError(f"Service {name} has an unknown dependency")
                condition = config.get("condition", "service_started")
                if condition not in _DEPENDENCY_CONDITIONS:
                    raise TemplateError(f"Service {name} has an invalid dependency condition")
                if condition == "service_healthy" and not services[dependency].get("healthcheck"):
                    raise TemplateError(
                        f"Service {name} requires {dependency} to be healthy, but it has no healthcheck"
                    )

    def _validate_component(self, name: str, component: Any) -> dict[str, Any]:
        if not isinstance(component, Mapping):
            raise TemplateError(f"Component {name} must be a mapping")

        images = component.get("images")
        if not isinstance(images, list) or not images or not all(isinstance(image, str) and image for image in images):
            raise TemplateError(f"Component {name} must define at least one image")
        if any(image.endswith(":latest") for image in images):
            raise TemplateError(f"Component {name} must not use an unpinned latest image")

        raw_ports = component.get("ports")
        if raw_ports is None:
            raw_ports = [component.get("port")]
        if not isinstance(raw_ports, list) or not raw_ports:
            raise TemplateError(f"Component {name} must define one or more ports")
        ports: list[int] = []
        for port in raw_ports:
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise TemplateError(f"Component {name} has an invalid port")
            ports.append(port)

        environment = self._normalize_environment(component.get("environment"), f"component {name} environment")
        healthcheck = component.get("healthcheck")
        if healthcheck is not None and not isinstance(healthcheck, Mapping):
            raise TemplateError(f"Component {name} healthcheck must be a mapping")
        connection = component.get("connection", {})
        if not isinstance(connection, Mapping):
            raise TemplateError(f"Component {name} connection must be a mapping")
        connection_environment = connection.get("environment", {})
        if not isinstance(connection_environment, Mapping):
            raise TemplateError(f"Component {name} connection environment must be a mapping")
        for key, value in connection_environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TemplateError(f"Component {name} connection environment must contain strings")

        raw_volumes = component.get("volumes", [])
        if not isinstance(raw_volumes, list):
            raise TemplateError(f"Component {name} volumes must be a list")
        volumes: list[dict[str, str]] = []
        for volume in raw_volumes:
            if not isinstance(volume, Mapping):
                raise TemplateError(f"Component {name} volume must be a mapping")
            source, target = volume.get("source"), volume.get("target")
            if not isinstance(source, str) or not source or not isinstance(target, str) or not target.startswith("/"):
                raise TemplateError(f"Component {name} volume requires source and absolute target")
            volumes.append({"source": source, "target": target})

        command = component.get("command")
        if command is not None and not isinstance(command, (str, list)):
            raise TemplateError(f"Component {name} command must be a string or list")
        working_dir = component.get("working_dir")
        if working_dir is not None and (not isinstance(working_dir, str) or not working_dir.startswith("/")):
            raise TemplateError(f"Component {name} working_dir must be an absolute path")
        restart = component.get("restart")
        if restart is not None and not isinstance(restart, str):
            raise TemplateError(f"Component {name} restart must be a string")

        return {
            "images": list(images),
            # Retain the original single-port metadata for callers that read
            # catalog entries directly; multi-port services use the first port.
            "port": ports[0],
            "ports": ports,
            "publish_ports": self._validate_boolean(
                component.get("publish_ports", True),
                f"Component {name} publish_ports",
            ),
            "environment": environment,
            "healthcheck": deepcopy(dict(healthcheck or {})),
            "connection": {"environment": dict(connection_environment)},
            "volumes": volumes,
            "command": deepcopy(command),
            "working_dir": working_dir,
            "restart": restart,
        }

    def _validate_selection(self, components: list[str]) -> list[str]:
        if not isinstance(components, list) or not components:
            raise TemplateError("At least one component is required")
        if not all(isinstance(name, str) for name in components):
            raise TemplateError("Component names must be strings")
        selected = [self._canonical_component(name) for name in components]
        if len(set(selected)) != len(selected):
            raise TemplateError("Duplicate components are not allowed")
        unknown = sorted(name for name, canonical in zip(components, selected) if canonical not in self.components)
        if unknown:
            raise TemplateError(f"Unknown components: {', '.join(unknown)}")
        return sorted(selected)

    @staticmethod
    def _validate_appid(appid: str | None) -> str | None:
        if appid is None:
            return None
        if not isinstance(appid, str) or not _APPID_PATTERN.fullmatch(appid):
            raise TemplateError("appid must contain only letters, numbers, underscores, and hyphens")
        return appid.lower()

    def _normalize_service_environments(
        self,
        selected: list[str],
        namespace: str | None,
        singular: Mapping[str, Mapping[str, Any]] | None,
        plural: Mapping[str, Mapping[str, Any]] | None,
    ) -> dict[str, dict[str, str]]:
        if singular is not None and plural is not None:
            raise TemplateError("Use either service_environment or service_environments, not both")
        raw_overrides = singular if singular is not None else plural
        if raw_overrides is None:
            return {}
        if not isinstance(raw_overrides, Mapping):
            raise TemplateError("service_environment must be a mapping")

        rendered_names = {self._service_name(name, namespace): name for name in selected}
        overrides: dict[str, dict[str, str]] = {}
        for service_name, values in raw_overrides.items():
            if not isinstance(service_name, str):
                raise TemplateError("service_environment keys must be service names")
            canonical_name = self._canonical_component(service_name)
            component_name = canonical_name if canonical_name in selected else rendered_names.get(service_name)
            if component_name is None:
                raise TemplateError("service_environment must reference selected components")
            overrides.setdefault(component_name, {}).update(
                self._normalize_environment(values, f"service environment for {service_name}")
            )
        return overrides

    def _normalize_dependencies(
        self,
        selected: list[str],
        dependencies: Mapping[str, Sequence[str] | Mapping[str, Any]] | None,
    ) -> dict[str, dict[str, dict[str, Any] | None]]:
        if dependencies is None:
            dependencies = {}
        if not isinstance(dependencies, Mapping):
            raise TemplateError("Dependencies must be a mapping")

        selected_set = set(selected)
        normalized: dict[str, dict[str, dict[str, Any] | None]] = {name: {} for name in selected}
        for name, required in dependencies.items():
            if not isinstance(name, str):
                raise TemplateError("Dependencies must reference selected components")
            name = self._canonical_component(name)
            if name not in selected_set:
                raise TemplateError("Dependencies must reference selected components")
            if isinstance(required, (str, bytes)) or not isinstance(required, (Sequence, Mapping)):
                raise TemplateError("Dependencies must be lists or mappings")

            items = (
                required.items()
                if isinstance(required, Mapping)
                else ((dependency, None) for dependency in required)
            )
            for dependency, config in items:
                if not isinstance(dependency, str):
                    raise TemplateError("Dependencies must reference selected components")
                dependency = self._canonical_component(dependency)
                if dependency not in selected_set:
                    raise TemplateError("Dependencies must reference selected components")
                if dependency in normalized[name]:
                    raise TemplateError("Duplicate dependencies are not allowed")
                normalized[name][dependency] = self._normalize_dependency_config(config)
        return {
            name: {dependency: config for dependency, config in sorted(required.items())}
            for name, required in sorted(normalized.items())
        }

    @staticmethod
    def _normalize_dependency_config(config: Any) -> dict[str, Any] | None:
        if config is None:
            return None
        if isinstance(config, str):
            config = {"condition": config}
        if not isinstance(config, Mapping):
            raise TemplateError("Dependency configuration must be a condition or mapping")
        normalized = deepcopy(dict(config))
        condition = normalized.get("condition", "service_started")
        if condition not in _DEPENDENCY_CONDITIONS:
            raise TemplateError(f"Invalid dependency condition: {condition}")
        normalized["condition"] = condition
        return normalized

    def _assert_acyclic(
        self,
        selected: list[str],
        dependencies: Mapping[str, Mapping[str, dict[str, Any] | None]],
    ) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise TemplateError("Component dependencies contain a cycle")
            if name in visited:
                return
            visiting.add(name)
            for dependency in dependencies[name]:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in selected:
            visit(name)

    def _internal_connection_environment(
        self,
        appid: str | None,
        dependencies: Mapping[str, dict[str, Any] | None],
        service_names: Mapping[str, str],
    ) -> dict[str, str]:
        environment: dict[str, str] = {}
        for dependency in dependencies:
            component = self.components[dependency]
            host = service_names[dependency]
            templates = component["connection"]["environment"]
            if not templates:
                prefix = re.sub(r"[^A-Za-z0-9]", "_", dependency).upper()
                templates = {f"{prefix}_HOST": "{host}", f"{prefix}_PORT": "{port}"}
            environment.update(
                self._render_environment_values(
                    templates,
                    appid=appid or "",
                    component=dependency,
                    host=host,
                    port=component["ports"][0],
                )
            )
        return environment

    def _render_dependencies(
        self,
        name: str,
        dependencies: Mapping[str, dict[str, Any] | None],
        service_names: Mapping[str, str],
        dependency_conditions: bool,
    ) -> list[str] | dict[str, dict[str, Any]] | None:
        if not dependencies:
            return None
        use_long_syntax = dependency_conditions or any(config is not None for config in dependencies.values())
        if not use_long_syntax:
            return [service_names[dependency] for dependency in dependencies]

        rendered: dict[str, dict[str, Any]] = {}
        for dependency, config in dependencies.items():
            if config is None:
                condition = "service_healthy" if self.components[dependency]["healthcheck"] else "service_started"
                config = {"condition": condition}
            if config["condition"] == "service_healthy" and not self.components[dependency]["healthcheck"]:
                raise TemplateError(
                    f"Service {name} requires {dependency} to be healthy, but it has no healthcheck"
                )
            rendered[service_names[dependency]] = deepcopy(config)
        return rendered

    @staticmethod
    def _service_name(component: str, namespace: str | None) -> str:
        return f"{namespace}-{component}" if namespace else component

    @staticmethod
    def _render_volumes(
        component: Mapping[str, Any], service_name: str
    ) -> tuple[list[str], dict[str, dict[str, Any]]]:
        rendered: list[str] = []
        declared: dict[str, dict[str, Any]] = {}
        for volume in component["volumes"]:
            volume_name = f"{service_name}-{volume['source']}"
            rendered.append(f"{volume_name}:{volume['target']}")
            declared[volume_name] = {}
        return rendered, declared

    @staticmethod
    def _normalize_environment(values: Mapping[str, Any] | None, label: str) -> dict[str, str]:
        if values is None:
            return {}
        if not isinstance(values, Mapping):
            raise TemplateError(f"{label} must be a mapping")
        normalized: dict[str, str] = {}
        for key, value in values.items():
            if not isinstance(key, str) or not key:
                raise TemplateError(f"{label} keys must be non-empty strings")
            if value is None or not isinstance(value, _SCALAR_TYPES):
                raise TemplateError(f"{label} values must be scalar values")
            normalized[key] = str(value).lower() if isinstance(value, bool) else str(value)
        return TemplateCatalog._ordered_mapping(normalized)

    @staticmethod
    def _render_environment_values(values: Mapping[str, str], **replacements: Any) -> dict[str, str]:
        rendered: dict[str, str] = {}
        for key, value in values.items():
            for replacement_name, replacement_value in replacements.items():
                value = value.replace(f"{{{replacement_name}}}", str(replacement_value))
            rendered[key] = value
        return rendered

    def _canonical_component(self, name: str) -> str:
        if name in self.components:
            return name
        lowered = name.lower()
        return self.aliases.get(lowered, lowered)

    @staticmethod
    def _validate_boolean(value: Any, label: str) -> bool:
        if not isinstance(value, bool):
            raise TemplateError(f"{label} must be a boolean")
        return value

    @staticmethod
    def _ordered_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
        return {key: values[key] for key in sorted(values)}
