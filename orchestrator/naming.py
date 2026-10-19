"""Validation and deterministic names for app-scoped external resources.

``appid`` is a domain identifier and is deliberately kept exactly as the
caller supplied it.  External systems have stricter, usually lowercase,
name rules, so callers that create a Docker/registry resource should use
``external_namespace`` rather than applying an ad-hoc ``lower()``.

The namespace conversion accepts a *raw* business app id only.  It is not
idempotent: an already encoded namespace may itself be a valid business id,
but is encoded again when passed as a raw app id. The reserved prefix keeps
the unchanged and encoded output spaces disjoint.
"""

from __future__ import annotations

import ipaddress
import json
import re
from hashlib import sha256
from typing import Final


APPID_MAX_LENGTH: Final = 64
APPID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
DOCKER_COMPONENT_PATTERN: Final = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$"
)
NAMESPACE_PREFIX: Final = "appid-v1-"
MAX_DOCKER_RESOURCE_NAME_LENGTH: Final = 63

# A registry host is either a DNS name, localhost, or an IPv6 literal.  A
# bare single-label host is intentionally rejected: Docker interprets it as
# a Docker Hub namespace rather than as a private registry host.
_REGISTRY_HOST_COMPONENT = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_REGISTRY_HOST_PATTERN = re.compile(
    rf"^(?:localhost|{_REGISTRY_HOST_COMPONENT}(?:\.{_REGISTRY_HOST_COMPONENT})*|\[[0-9A-Fa-f:]+\])"
    r"(?::[0-9]{1,5})?$"
)
_REPOSITORY_PATH_PATTERN = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*(?:/"
    r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*)*$"
)


class NamingError(ValueError):
    """Raised when an app id or external image/resource name is invalid."""


def validate_appid(value: str) -> str:
    """Validate and return a raw business app id without changing it."""

    if not isinstance(value, str):
        raise NamingError("appid must be a string")
    if not 1 <= len(value) <= APPID_MAX_LENGTH:
        raise NamingError(f"appid must be between 1 and {APPID_MAX_LENGTH} characters")
    if APPID_PATTERN.fullmatch(value) is None:
        raise NamingError("appid must start with a letter or number and contain only letters, numbers, underscores, and hyphens")
    return value


def is_docker_repository_component(value: str) -> bool:
    """Return whether ``value`` is a lowercase Docker repository component."""

    return isinstance(value, str) and DOCKER_COMPONENT_PATTERN.fullmatch(value) is not None


def external_namespace(appid: str) -> str:
    """Map a raw business app id to a collision-free external namespace.

    Existing canonical lowercase Docker components keep their exact spelling
    for compatibility.  Every other valid app id is represented by a versioned
    prefix plus the original UTF-8 bytes in hexadecimal, preserving case and
    punctuation without relying on lossy substitutions.
    """

    raw = validate_appid(appid)
    if not raw.startswith(NAMESPACE_PREFIX) and is_docker_repository_component(raw):
        return raw
    return f"{NAMESPACE_PREFIX}{raw.encode('utf-8').hex()}"


def validate_build_id(build_id: str) -> str:
    if not isinstance(build_id, str) or re.fullmatch(r"[a-f0-9]{32}", build_id) is None:
        raise NamingError("Build identifier must contain 32 lowercase hexadecimal characters")
    return build_id


def build_namespace(appid: str, build_id: str) -> str:
    identity = json.dumps([validate_appid(appid), validate_build_id(build_id)], separators=(",", ":"))
    return f"build-v1-{sha256(identity.encode('utf-8')).hexdigest()[:32]}"


def bounded_resource_name(prefix: str, suffix: str | None = None, *, max_length: int = MAX_DOCKER_RESOURCE_NAME_LENGTH) -> str:
    """Build a deterministic bounded Docker resource name.

    The unshortened form is preserved whenever possible.  When shortening is
    necessary, a digest of the complete raw name prevents two long app/service
    combinations from collapsing to the same resource name.
    """

    if not isinstance(prefix, str) or not prefix:
        raise NamingError("resource name prefix must be a non-empty string")
    if suffix is not None and (not isinstance(suffix, str) or not suffix):
        raise NamingError("resource name suffix must be a non-empty string")
    if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length < 1:
        raise NamingError("resource name max_length must be a positive integer")

    raw = prefix if suffix is None else f"{prefix}-{suffix}"
    if len(raw) <= max_length:
        return raw

    digest = sha256(raw.encode("utf-8")).hexdigest()[:10]
    if max_length <= len(digest) + 1:
        return digest[:max_length]
    prefix_length = max_length - len(digest) - 1
    return f"{raw[:max(0, prefix_length)]}-{digest}"[-max_length:]


def managed_volume_name(namespace: str, alias: str) -> str:
    """Name an implicit app-owned volume from its complete identity pair.

    Joining names with a hyphen is not an ownership boundary: ``foo`` with
    ``bar-data`` and ``foo-bar`` with ``data`` otherwise share a volume. A
    versioned, length-bounded name includes a 128-bit digest of the separately
    serialized namespace and logical Compose alias. It is computed only for
    default volumes; explicit ``name``/``external`` declarations retain their
    deliberate sharing semantics.
    """

    if not is_docker_repository_component(namespace):
        raise NamingError("managed volume namespace must be a Docker repository component")
    if not isinstance(alias, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", alias) is None:
        raise NamingError("managed volume alias must be a Docker resource name")
    return _volume_identity_name("vol-v1", namespace, alias)


def template_volume_alias(component: str, source: str) -> str:
    """Preserve component/source boundaries before an app is selected.

    A generic template is reusable across applications. Its implicit aliases
    must not collapse distinct component/source pairs, and must yield the same
    app-scoped physical volume as the corresponding app-bound template.
    """

    for value in (component, source):
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None:
            raise NamingError("template volume component and source must be Docker resource names")
    return _volume_identity_name("tplvol-v1", component, source)


def _volume_identity_name(kind: str, first: str, second: str) -> str:
    identity = json.dumps([first, second], ensure_ascii=True, separators=(",", ":"))
    digest = sha256(identity.encode("ascii")).hexdigest()[:32]
    readable = f"{kind}-{first}-{second}".lower()
    prefix_budget = MAX_DOCKER_RESOURCE_NAME_LENGTH - len(digest) - 1
    return f"{readable[:prefix_budget]}-{digest}"


def _validate_registry_host(host: str) -> str:
    if not host or _REGISTRY_HOST_PATTERN.fullmatch(host) is None:
        raise NamingError("image repository registry host is invalid")
    if host.startswith("["):
        closing = host.find("]")
        port = host[closing + 1 :]
        try:
            ipaddress.IPv6Address(host[1:closing])
        except ValueError:
            raise NamingError("image repository registry IPv6 address is invalid") from None
        if port and (not port.startswith(":") or not port[1:].isdigit() or not 1 <= int(port[1:]) <= 65535):
            raise NamingError("image repository registry port is invalid")
    elif ":" in host:
        port = host.rsplit(":", 1)[1]
        if not 1 <= int(port) <= 65535:
            raise NamingError("image repository registry port is invalid")
    elif host.lower() != "localhost" and "." not in host:
        raise NamingError("image repository registry host must be qualified or include a port")
    return host


def validate_image_repository(value: str) -> str:
    """Validate a fully qualified ``registry/repository`` image reference.

    Tags and digests are deliberately rejected: this value is the repository
    passed to ``docker build -t`` and receives a tag later in the pipeline.
    The registry host remains case-preserving, while every repository path
    component must be lowercase and Docker-compatible.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        raise NamingError("IMAGE_REPOSITORY must be a registry/repository reference")
    if any(marker in value for marker in ("://", "?", "#", "@")):
        raise NamingError("IMAGE_REPOSITORY must be a registry/repository reference")
    if value.startswith("/") or "//" in value:
        raise NamingError("IMAGE_REPOSITORY must be a registry/repository reference")

    parts = value.split("/")
    if len(parts) < 2 or any(not part for part in parts):
        raise NamingError("IMAGE_REPOSITORY must be a registry/repository reference")
    _validate_registry_host(parts[0])
    repository = "/".join(parts[1:])
    if len(repository) > 255 or _REPOSITORY_PATH_PATTERN.fullmatch(repository) is None:
        raise NamingError("IMAGE_REPOSITORY repository path is invalid")
    return value


def registry_from_image_repository(value: str) -> str:
    """Return the validated registry host from a repository reference."""

    validated = validate_image_repository(value)
    return validated.split("/", 1)[0]
