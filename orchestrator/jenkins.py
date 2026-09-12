"""Jenkins HTTP adapter for the orchestration build lifecycle.

Network I/O is isolated behind ``HttpTransport`` so production uses a small
stdlib transport while tests can provide deterministic fakes.  The adapter
never includes credentials or submitted environment values in exception text.
"""

from __future__ import annotations

import json
import re
from base64 import b64encode
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol
from urllib.parse import quote, urlencode, urljoin, urlsplit

from orchestrator.adapters import AdapterError
from orchestrator.gitlab import HttpResponse, HttpTransport, UrllibHttpTransport


class JenkinsError(AdapterError):
    """A Jenkins operation failed with context safe for events and alerts."""


class JenkinsNotFoundError(JenkinsError):
    """The requested Jenkins queue item or build does not exist."""


class JenkinsArtifactError(JenkinsError):
    """The required Jenkins result artifact is absent or invalid."""


class JenkinsBuildState(str, Enum):
    BUILDING = "building"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class JenkinsBuildRequest:
    """All values passed to the parameterized Jenkins pipeline job."""

    appid: str
    repository_url: str
    git_ref: str
    environment: Mapping[str, str]
    compose: Mapping[str, Any] | None
    image_repository: str

    def __post_init__(self) -> None:
        for name in ("appid", "repository_url", "git_ref", "image_repository"):
            _require_nonempty(getattr(self, name), name)
        if not isinstance(self.environment, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.environment.items()
        ):
            raise ValueError("environment must be a mapping of strings")
        if self.compose is not None and not isinstance(self.compose, Mapping):
            raise ValueError("compose must be a mapping when provided")

    def __repr__(self) -> str:
        # Compose content may itself contain environment values, so expose only
        # its presence and the environment variable names for safe diagnostics.
        return (
            "JenkinsBuildRequest("
            f"appid={self.appid!r}, repository_url={_redact_url(self.repository_url)!r}, "
            f"git_ref={self.git_ref!r}, environment_keys={sorted(self.environment)!r}, "
            f"compose={'provided' if self.compose is not None else 'repository'}, "
            f"image_repository={self.image_repository!r})"
        )

    def as_parameters(self) -> dict[str, str]:
        """Serialize the stable parameter contract expected by the Jenkins job."""

        try:
            environment = json.dumps(dict(self.environment), sort_keys=True, separators=(",", ":"))
            compose = "" if self.compose is None else json.dumps(dict(self.compose), sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            raise JenkinsError("Build input could not be serialized for Jenkins") from None
        return {
            "APPID": self.appid,
            "REPOSITORY_URL": self.repository_url,
            "GIT_REF": self.git_ref,
            "ENVIRONMENT_JSON": environment,
            "COMPOSE_JSON": compose,
            "IMAGE_REPOSITORY": self.image_repository,
        }


@dataclass(frozen=True)
class JenkinsQueueItem:
    queue_url: str
    queue_id: int | None
    build_number: int | None = None
    cancelled: bool = False
    why: str | None = None

    @property
    def pending(self) -> bool:
        return self.build_number is None and not self.cancelled


@dataclass(frozen=True)
class JenkinsBuild:
    number: int
    building: bool
    result: str | None
    url: str | None = None

    @property
    def state(self) -> JenkinsBuildState:
        if self.building:
            return JenkinsBuildState.BUILDING
        if self.result == "SUCCESS":
            return JenkinsBuildState.SUCCEEDED
        if self.result == "ABORTED":
            return JenkinsBuildState.CANCELLED
        if self.result in {"FAILURE", "UNSTABLE", "NOT_BUILT"}:
            return JenkinsBuildState.FAILED
        return JenkinsBuildState.UNKNOWN

    @property
    def status(self) -> JenkinsBuildState:
        """Alias used by pipeline code that speaks in domain status terms."""

        return self.state

    @property
    def terminal(self) -> bool:
        return not self.building and self.state is not JenkinsBuildState.UNKNOWN


@dataclass(frozen=True, eq=False)
class JenkinsResultArtifact(Mapping[str, Any]):
    """Validated ``orchestrator-result.json`` data with mapping compatibility."""

    images: tuple[str, ...]
    compose: Mapping[str, Any] = field(repr=False)
    _payload: Mapping[str, Any] = field(repr=False)

    def __repr__(self) -> str:
        return f"JenkinsResultArtifact(images={list(self.images)!r}, compose='***')"

    def __getitem__(self, key: str) -> Any:
        return self._payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._payload)

    def __len__(self) -> int:
        return len(self._payload)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, JenkinsResultArtifact):
            return self._payload == other._payload
        if isinstance(other, Mapping):
            return dict(self._payload) == dict(other)
        return NotImplemented

    def as_dict(self) -> dict[str, Any]:
        return dict(self._payload)


class JenkinsAdapter(Protocol):
    def trigger_build(self, request: JenkinsBuildRequest) -> JenkinsQueueItem: ...

    def get_queue_item(self, queue_url: str) -> JenkinsQueueItem: ...

    def get_build(self, number: int) -> JenkinsBuild: ...

    def get_result_artifact(self, number: int) -> JenkinsResultArtifact: ...


class JenkinsHttpAdapter:
    """Concrete Jenkins adapter for parameterized jobs, queue polling and artifacts."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        job_name: str = "apps-orchestrator",
        *,
        transport: HttpTransport | None = None,
        timeout: float = 30,
    ) -> None:
        self._base_url = _normalise_base_url(base_url)
        self._username = _require_nonempty(username, "username")
        self._password = _require_nonempty(password, "password")
        self._job_path = _job_path(job_name)
        self._transport = transport or UrllibHttpTransport(timeout)
        credentials = f"{self._username}:{self._password}".encode("utf-8")
        self._authorization = f"Basic {b64encode(credentials).decode('ascii')}"
        base_parts = urlsplit(self._base_url)
        self._origin = (base_parts.scheme.lower(), base_parts.hostname.lower(), _port(base_parts))
        self._base_path = base_parts.path.rstrip("/")

    def __repr__(self) -> str:
        return f"JenkinsHttpAdapter(base_url={self._base_url!r}, username='***', password='***', job_name={self._job_name!r})"

    @classmethod
    def from_settings(cls, settings: Any, *, transport: HttpTransport | None = None) -> "JenkinsHttpAdapter":
        """Construct the adapter from settings while retaining an injectable transport."""

        if not getattr(settings, "jenkins_url", None):
            raise ValueError("Jenkins URL must be configured")
        if not getattr(settings, "jenkins_user", None) or not getattr(settings, "jenkins_password", None):
            raise ValueError("Jenkins username and password must be configured")
        return cls(
            settings.jenkins_url,
            settings.jenkins_user,
            settings.jenkins_password,
            job_name=getattr(settings, "jenkins_job_name", "apps-orchestrator"),
            transport=transport,
            timeout=getattr(settings, "external_request_timeout_seconds", 30),
        )

    @property
    def _job_name(self) -> str:
        return "/".join(segment for segment in self._job_path.split("/") if segment and segment != "job")

    def trigger_build(self, request: JenkinsBuildRequest) -> JenkinsQueueItem:
        """Request a parameterized build and return the Jenkins queue reference."""

        if not isinstance(request, JenkinsBuildRequest):
            raise TypeError("request must be a JenkinsBuildRequest")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            **self._crumb_headers(),
        }
        body = urlencode(request.as_parameters()).encode("utf-8")
        response = self._request("POST", f"{self._job_path}/buildWithParameters", headers=headers, body=body)
        if response.status_code not in {200, 201, 202, 302}:
            self._raise_for_status(response, "build trigger")
        queue_url = response.header("Location")
        if not queue_url:
            raise JenkinsError("Jenkins did not return a queue location")
        queue_url = self._normalise_queue_url(queue_url)
        return JenkinsQueueItem(queue_url=queue_url, queue_id=_queue_id_from_url(queue_url))

    def get_queue_item(self, queue_url: str) -> JenkinsQueueItem:
        """Read a queue item to learn whether it is pending, cancelled or executable."""

        normalised_url = self._normalise_queue_url(queue_url)
        response = self._request("GET", f"{normalised_url.rstrip('/')}/api/json")
        if response.status_code != 200:
            self._raise_for_status(response, "queue item")
        payload = self._json_object(response, "queue item")
        executable = payload.get("executable")
        if executable is not None and not isinstance(executable, Mapping):
            raise JenkinsError("Jenkins returned an invalid queue item")
        build_number = _optional_positive_int(executable.get("number") if executable else None, "build number")
        cancelled = payload.get("cancelled", False)
        if not isinstance(cancelled, bool):
            raise JenkinsError("Jenkins returned an invalid queue item")
        why = payload.get("why")
        if why is not None and not isinstance(why, str):
            raise JenkinsError("Jenkins returned an invalid queue item")
        return JenkinsQueueItem(
            queue_url=normalised_url,
            queue_id=_optional_positive_int(payload.get("id"), "queue id") or _queue_id_from_url(normalised_url),
            build_number=build_number,
            cancelled=cancelled,
            why=why,
        )

    def get_build(self, number: int) -> JenkinsBuild:
        """Read the current Jenkins state for a concrete build number."""

        build_number = _positive_int(number, "number")
        response = self._request("GET", f"{self._job_path}/{build_number}/api/json")
        if response.status_code != 200:
            self._raise_for_status(response, "build")
        payload = self._json_object(response, "build")
        building = payload.get("building")
        result = payload.get("result")
        if not isinstance(building, bool) or result is not None and not isinstance(result, str):
            raise JenkinsError("Jenkins returned an invalid build response")
        url = payload.get("url")
        if url is not None and not isinstance(url, str):
            raise JenkinsError("Jenkins returned an invalid build response")
        return JenkinsBuild(number=build_number, building=building, result=result, url=url)

    def get_result_artifact(self, number: int) -> JenkinsResultArtifact:
        """Fetch and validate the mandatory ``orchestrator-result.json`` artifact."""

        build_number = _positive_int(number, "number")
        response = self._request("GET", f"{self._job_path}/{build_number}/artifact/orchestrator-result.json")
        if response.status_code == 404:
            raise JenkinsArtifactError("Jenkins result artifact is missing")
        if response.status_code != 200:
            self._raise_for_status(response, "result artifact")
        payload = self._json_object(response, "result artifact", error_type=JenkinsArtifactError)
        images = payload.get("images")
        compose = payload.get("compose")
        if (
            not isinstance(images, list)
            or not images
            or not all(isinstance(image, str) and image.strip() for image in images)
            or not isinstance(compose, Mapping)
        ):
            raise JenkinsArtifactError("Jenkins result artifact has an invalid format")
        return JenkinsResultArtifact(images=tuple(images), compose=compose, _payload=payload)

    def _crumb_headers(self) -> dict[str, str]:
        response = self._request("GET", "/crumbIssuer/api/json")
        if response.status_code == 404:
            # Jenkins can disable CSRF protection; a missing issuer is then a
            # valid response and the build request proceeds without a crumb.
            return {}
        if response.status_code != 200:
            self._raise_for_status(response, "crumb request")
        payload = self._json_object(response, "crumb request")
        field_name = payload.get("crumbRequestField")
        crumb = payload.get("crumb")
        if not isinstance(field_name, str) or not isinstance(crumb, str) or not field_name or not crumb:
            raise JenkinsError("Jenkins returned an invalid crumb response")
        if "\r" in field_name or "\n" in field_name or "\r" in crumb or "\n" in crumb:
            raise JenkinsError("Jenkins returned an invalid crumb response")
        return {field_name: crumb}

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else self._url(path_or_url)
        request_headers = {"Accept": "application/json", "Authorization": self._authorization}
        if headers:
            request_headers.update(headers)
        try:
            return self._transport.request(method, url, headers=request_headers, body=body)
        except Exception as exc:
            if isinstance(exc, JenkinsError):
                raise
            raise JenkinsError("Jenkins request failed") from None

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _normalise_queue_url(self, queue_url: str) -> str:
        raw = _require_nonempty(queue_url, "queue_url")
        if raw.startswith("//"):
            raise JenkinsError("Jenkins returned an invalid queue location")
        try:
            parsed = urlsplit(raw)
        except ValueError:
            raise JenkinsError("Jenkins returned an invalid queue location") from None
        if parsed.username or parsed.password:
            raise JenkinsError("Jenkins returned an invalid queue location")
        target = raw if parsed.scheme else urljoin(f"{self._base_url}/", raw)
        try:
            target_parts = urlsplit(target)
        except ValueError:
            raise JenkinsError("Jenkins returned an invalid queue location") from None
        if target_parts.query or target_parts.fragment:
            raise JenkinsError("Jenkins returned an invalid queue location")
        target_origin = (target_parts.scheme.lower(), (target_parts.hostname or "").lower(), _port(target_parts))
        if target_origin != self._origin:
            raise JenkinsError("Jenkins returned a queue location outside the configured host")
        queue_path = target_parts.path.rstrip("/") + "/"
        expected_queue_prefixes = ["/queue/item/"]
        if self._base_path:
            expected_queue_prefixes.append(f"{self._base_path}/queue/item/")
        if not any(queue_path.startswith(prefix) for prefix in expected_queue_prefixes):
            raise JenkinsError("Jenkins returned an invalid queue location")
        return target.rstrip("/") + "/"

    def _json_object(
        self,
        response: HttpResponse,
        operation: str,
        *,
        error_type: type[JenkinsError] = JenkinsError,
    ) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            raise error_type(f"Jenkins returned an invalid {operation} response") from None
        if not isinstance(payload, Mapping):
            raise error_type(f"Jenkins returned an invalid {operation} response")
        return payload

    @staticmethod
    def _raise_for_status(response: HttpResponse, operation: str) -> None:
        if response.status_code == 404:
            raise JenkinsNotFoundError(f"Jenkins {operation} was not found")
        raise JenkinsError(f"Jenkins {operation} failed (HTTP {response.status_code})")


def _normalise_base_url(base_url: str) -> str:
    raw = _require_nonempty(base_url, "base_url")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise ValueError("base_url must be a valid HTTP(S) URL") from None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("base_url must be a credential-free HTTP(S) URL")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url cannot contain a query or fragment")
    return raw.rstrip("/")


def _job_path(job_name: str) -> str:
    raw = _require_nonempty(job_name, "job_name").strip("/")
    segments = raw.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("job_name must contain non-empty path segments")
    return "/".join(f"job/{quote(segment, safe='')}" for segment in segments)


def _require_nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, name)


def _queue_id_from_url(queue_url: str) -> int | None:
    match = re.search(r"/queue/item/(\d+)(?:/|$)", urlsplit(queue_url).path)
    return int(match.group(1)) if match else None


def _port(parts) -> int | None:
    try:
        return parts.port
    except ValueError as exc:
        raise ValueError("URL contains an invalid port") from exc


def _redact_url(value: str) -> str:
    """Keep a diagnostic repository URL useful without exposing user info."""

    try:
        parts = urlsplit(value)
    except ValueError:
        return "***"
    if not parts.scheme or not parts.netloc or not (parts.username or parts.password):
        return value
    hostname = parts.hostname or ""
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port = ""
    return f"{parts.scheme}://{hostname}{port}{parts.path}"


# A concise alias is useful for callers that do not need to distinguish the
# implementation from the adapter boundary.
JenkinsClient = JenkinsHttpAdapter
