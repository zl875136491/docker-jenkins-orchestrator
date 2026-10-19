"""Shared source-Compose rules for local Dockerfile builds."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any


class ComposeBuildContractError(ValueError):
    """Raised when a source Compose document cannot be built locally."""


def validate_local_build_compose(compose: Any) -> None:
    """Require every source service to use an explicit local Dockerfile build."""

    if not isinstance(compose, Mapping):
        raise ComposeBuildContractError("Compose document must be a mapping")
    services = compose.get("services")
    if not isinstance(services, Mapping) or not services:
        raise ComposeBuildContractError("Compose document must contain a non-empty services mapping")
    for name, service in services.items():
        if not isinstance(name, str) or not isinstance(service, Mapping):
            raise ComposeBuildContractError("Compose services must be named mappings")
        normalize_local_build(service.get("build"), name)


def normalize_local_build(build: Any, service_name: str) -> dict[str, Any]:
    """Validate and return a copy of one service's explicit build definition."""

    if not isinstance(build, Mapping):
        raise ComposeBuildContractError(
            f"Compose service {service_name} must use local build mode with build.context and build.dockerfile"
        )
    context = build.get("context")
    dockerfile = build.get("dockerfile")
    _validate_relative_build_path(context, f"Compose service {service_name} build.context")
    _validate_relative_build_path(dockerfile, f"Compose service {service_name} build.dockerfile")
    normalized = dict(build)
    normalized["context"] = context
    normalized["dockerfile"] = dockerfile
    return normalized


def validate_local_build_files(compose: Any, source_root: Path) -> None:
    """Validate that every declared build context and Dockerfile exists."""

    validate_local_build_compose(compose)
    root = Path(source_root)
    if not root.is_dir():
        raise ComposeBuildContractError(f"Compose source root does not exist: {root}")
    resolved_root = root.resolve()
    for name, service in compose["services"].items():
        build = normalize_local_build(service["build"], name)
        context_path = root / build["context"]
        dockerfile_path = context_path / build["dockerfile"]
        try:
            resolved_context = context_path.resolve()
            resolved_dockerfile = dockerfile_path.resolve()
            if not resolved_context.is_relative_to(resolved_root) or not resolved_dockerfile.is_relative_to(resolved_root):
                raise ComposeBuildContractError(
                    f"Compose service {name} build context or Dockerfile escapes the source root"
                )
        except OSError as exc:
            raise ComposeBuildContractError(f"Unable to inspect build files for service {name}") from exc
        if not context_path.is_dir():
            raise ComposeBuildContractError(f"Compose service {name} build context does not exist: {build['context']}")
        if not dockerfile_path.is_file():
            raise ComposeBuildContractError(
                f"Compose service {name} Dockerfile does not exist: {build['dockerfile']}"
            )


def _validate_relative_build_path(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ComposeBuildContractError(f"{label} is required")
    if "\\" in value or "\x00" in value or value.startswith("~"):
        raise ComposeBuildContractError(f"{label} must be a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ComposeBuildContractError(f"{label} must be a safe relative POSIX path")
