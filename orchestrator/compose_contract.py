"""Shared source-Compose rules for local Dockerfile builds."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any


class ComposeBuildContractError(ValueError):
    """Raised when a source Compose document cannot be built locally."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "COMPOSE_CONFIG_ERROR",
        subcode: str | None = None,
        service: str | None = None,
        config_path: str | None = None,
        remediation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.service = service
        self.config_path = config_path
        self.remediation = remediation
        detail: dict[str, Any] = {
            "code": code,
            "summary": message,
            "recognized": True,
        }
        if subcode:
            detail["subcode"] = subcode
        if service:
            detail["service"] = service
        if config_path:
            detail["config_path"] = config_path
        if remediation:
            detail["remediation"] = remediation
        self.error_detail = detail
        self.failure_site = "validating"


def validate_local_build_compose(compose: Any) -> None:
    """Require every source service to use an explicit local Dockerfile build."""

    if not isinstance(compose, Mapping):
        raise ComposeBuildContractError(
            "Compose document must be a mapping",
            subcode="INVALID_COMPOSE_DOCUMENT",
            remediation="请提交合法的 Compose YAML/JSON 对象（顶层为 mapping）。",
        )
    services = compose.get("services")
    if not isinstance(services, Mapping) or not services:
        raise ComposeBuildContractError(
            "Compose document must contain a non-empty services mapping",
            subcode="MISSING_SERVICES",
            config_path="services",
            remediation="请在 compose 中定义至少一个 services 条目。",
        )
    for name, service in services.items():
        if not isinstance(name, str) or not isinstance(service, Mapping):
            raise ComposeBuildContractError(
                "Compose services must be named mappings",
                subcode="INVALID_SERVICE_MAPPING",
                config_path="services",
                remediation="每个 service 必须是以服务名为键的 mapping。",
            )
        normalize_local_build(service.get("build"), name)


def normalize_local_build(build: Any, service_name: str) -> dict[str, Any]:
    """Validate and return a copy of one service's explicit build definition."""

    if not isinstance(build, Mapping):
        raise ComposeBuildContractError(
            f"Compose service {service_name} must use local build mode with build.context and build.dockerfile",
            subcode="LOCAL_BUILD_REQUIRED",
            service=service_name,
            config_path=f"services.{service_name}.build",
            remediation="请为该服务配置 build.context 与 build.dockerfile，平台仅支持本地 Dockerfile 构建。",
        )
    context = build.get("context")
    dockerfile = build.get("dockerfile")
    _validate_relative_build_path(context, f"Compose service {service_name} build.context", service_name, "context")
    _validate_relative_build_path(dockerfile, f"Compose service {service_name} build.dockerfile", service_name, "dockerfile")
    normalized = dict(build)
    normalized["context"] = context
    normalized["dockerfile"] = dockerfile
    return normalized


def validate_local_build_files(compose: Any, source_root: Path) -> None:
    """Validate that every declared build context and Dockerfile exists."""

    validate_local_build_compose(compose)
    root = Path(source_root)
    if not root.is_dir():
        raise ComposeBuildContractError(
            f"Compose source root does not exist: {root}",
            subcode="SOURCE_ROOT_MISSING",
            remediation="请确认构建源码根目录已正确上传或检出。",
        )
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
                    f"Compose service {name} build context or Dockerfile escapes the source root",
                    subcode="BUILD_PATH_ESCAPES_ROOT",
                    service=name,
                    config_path=f"services.{name}.build",
                    remediation="build.context / build.dockerfile 必须位于源码根目录内，禁止使用 .. 逃逸。",
                )
        except OSError as exc:
            raise ComposeBuildContractError(
                f"Unable to inspect build files for service {name}",
                subcode="BUILD_PATH_INSPECT_FAILED",
                service=name,
                config_path=f"services.{name}.build",
                remediation="无法检查构建路径，请确认文件权限与路径有效后重试。",
            ) from exc
        if not context_path.is_dir():
            raise ComposeBuildContractError(
                f"Compose service {name} build context does not exist: {build['context']}",
                subcode="BUILD_CONTEXT_MISSING",
                service=name,
                config_path=f"services.{name}.build.context",
                remediation=(
                    f"请检查服务 {name} 的 build.context={build['context']!r} 是否指向源码根目录内真实存在的目录。"
                ),
            )
        if not dockerfile_path.is_file():
            raise ComposeBuildContractError(
                f"Compose service {name} Dockerfile does not exist: {build['dockerfile']}",
                subcode="DOCKERFILE_MISSING",
                service=name,
                config_path=f"services.{name}.build.dockerfile",
                remediation=(
                    f"请确认相对 context 的 Dockerfile 路径 {build['dockerfile']!r} 存在，并注意大小写。"
                ),
            )


def _validate_relative_build_path(value: Any, label: str, service_name: str, field: str) -> None:
    config_path = f"services.{service_name}.build.{field}"
    if not isinstance(value, str) or not value.strip():
        raise ComposeBuildContractError(
            f"{label} is required",
            subcode="BUILD_PATH_REQUIRED",
            service=service_name,
            config_path=config_path,
            remediation=f"请为服务 {service_name} 填写 build.{field}。",
        )
    if "\\" in value or "\x00" in value or value.startswith("~"):
        raise ComposeBuildContractError(
            f"{label} must be a safe relative POSIX path",
            subcode="UNSAFE_BUILD_PATH",
            service=service_name,
            config_path=config_path,
            remediation="请使用相对 POSIX 路径（禁止 ~、反斜杠与空字节）。",
        )
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ComposeBuildContractError(
            f"{label} must be a safe relative POSIX path",
            subcode="UNSAFE_BUILD_PATH",
            service=service_name,
            config_path=config_path,
            remediation="请使用相对 POSIX 路径（禁止绝对路径与 ..）。",
        )
