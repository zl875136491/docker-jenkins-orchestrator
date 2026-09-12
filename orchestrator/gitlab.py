"""GitLab API adapter used by the build pipeline.

The adapter deliberately owns only the small verification surface the pipeline
needs: confirm a repository exists and resolve a requested Git reference.  The
repository checkout itself remains a Jenkins concern.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from orchestrator.adapters import AdapterError


class TransportError(RuntimeError):
    """Raised by the low-level HTTP transport without exposing request data."""


@dataclass(frozen=True)
class HttpResponse:
    """A minimal HTTP response shape that is convenient to fake in tests."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        expected = name.lower()
        for key, value in self.headers.items():
            if key.lower() == expected:
                return value
        return None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Response did not contain valid JSON") from exc


class HttpTransport(Protocol):
    """Transport seam shared by the external HTTP adapters."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse: ...


class UrllibHttpTransport:
    """Small production transport; tests should inject an ``HttpTransport`` fake."""

    def __init__(self, timeout: float = 30) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method.upper())
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except HTTPError as exc:
            # HTTP status failures are useful to the adapter and contain no
            # reason to surface a server body, which may contain sensitive data.
            return HttpResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read(),
            )
        except (OSError, TimeoutError, URLError, ValueError):
            raise TransportError("HTTP transport request failed") from None


class GitLabError(AdapterError):
    """A GitLab operation failed with a message safe for events and alerts."""


class GitLabNotFoundError(GitLabError):
    """GitLab did not find the requested project or revision."""


@dataclass(frozen=True)
class GitLabProject:
    project_id: int | str
    path_with_namespace: str
    web_url: str | None = None


@dataclass(frozen=True)
class GitLabRepositoryRef:
    """The GitLab project and immutable commit resolved for a submitted ref."""

    project: GitLabProject
    git_ref: str
    commit_sha: str
    repository_url: str = field(repr=False)


class GitLabAdapter(Protocol):
    def validate_repository_ref(self, repository_url: str, git_ref: str) -> GitLabRepositoryRef: ...


class GitLabHttpAdapter:
    """Validate repositories and refs through GitLab's v4 project API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: HttpTransport | None = None,
        timeout: float = 30,
    ) -> None:
        self._base_url = _normalise_base_url(base_url)
        if not isinstance(token, str) or not token.strip():
            raise ValueError("GitLab token must be configured")
        self._token = token
        self._transport = transport or UrllibHttpTransport(timeout)
        self._api_url = self._base_url if self._base_url.endswith("/api/v4") else f"{self._base_url}/api/v4"
        self._host = urlsplit(self._base_url).hostname

    def __repr__(self) -> str:
        return f"GitLabHttpAdapter(base_url={self._base_url!r}, token='***')"

    @classmethod
    def from_settings(cls, settings: Any, *, transport: HttpTransport | None = None) -> "GitLabHttpAdapter":
        """Construct the adapter from the application's settings without logging secrets."""

        if not getattr(settings, "gitlab_url", None) or not getattr(settings, "gitlab_token", None):
            raise ValueError("GitLab URL and token must be configured")
        return cls(
            settings.gitlab_url,
            settings.gitlab_token,
            transport=transport,
            timeout=getattr(settings, "external_request_timeout_seconds", 30),
        )

    def validate_repository_ref(self, repository_url: str, git_ref: str) -> GitLabRepositoryRef:
        """Confirm ``repository_url`` exists and resolve ``git_ref`` to a commit SHA."""

        project_path = self._project_path(repository_url)
        ref = _require_nonempty(git_ref, "git_ref")
        project = self._get_project(project_path)
        commit = self._get_commit(project.project_id, ref)
        commit_sha = commit.get("id")
        if not isinstance(commit_sha, str) or not commit_sha:
            raise GitLabError("GitLab returned an invalid commit response")
        return GitLabRepositoryRef(
            project=project,
            git_ref=ref,
            commit_sha=commit_sha,
            repository_url=repository_url,
        )

    def _get_project(self, project_path: str) -> GitLabProject:
        payload = self._request_json(
            f"/projects/{quote(project_path, safe='')}",
            not_found_message="GitLab repository was not found",
        )
        project_id = payload.get("id")
        path_with_namespace = payload.get("path_with_namespace")
        if project_id is None or not isinstance(path_with_namespace, str) or not path_with_namespace:
            raise GitLabError("GitLab returned an invalid project response")
        web_url = payload.get("web_url")
        return GitLabProject(
            project_id=project_id,
            path_with_namespace=path_with_namespace,
            web_url=web_url if isinstance(web_url, str) else None,
        )

    def _get_commit(self, project_id: int | str, git_ref: str) -> Mapping[str, Any]:
        return self._request_json(
            f"/projects/{quote(str(project_id), safe='')}/repository/commits/{quote(git_ref, safe='')}",
            not_found_message="GitLab reference was not found",
        )

    def _request_json(self, path: str, *, not_found_message: str) -> Mapping[str, Any]:
        try:
            response = self._transport.request(
                "GET",
                f"{self._api_url}/{path.lstrip('/')}",
                headers={"Accept": "application/json", "PRIVATE-TOKEN": self._token},
            )
        except Exception as exc:
            if isinstance(exc, GitLabError):
                raise
            raise GitLabError("GitLab request failed") from None
        if response.status_code == 404:
            raise GitLabNotFoundError(not_found_message)
        if not 200 <= response.status_code < 300:
            raise GitLabError(f"GitLab request failed (HTTP {response.status_code})")
        try:
            payload = response.json()
        except ValueError:
            raise GitLabError("GitLab returned an invalid JSON response") from None
        if not isinstance(payload, Mapping):
            raise GitLabError("GitLab returned an invalid JSON response")
        return payload

    def _project_path(self, repository_url: str) -> str:
        raw = _require_nonempty(repository_url, "repository_url")
        host: str | None = None
        path = raw
        if raw.startswith(("http://", "https://", "ssh://")):
            try:
                parsed = urlsplit(raw)
            except ValueError:
                raise GitLabError("Repository URL is invalid") from None
            if parsed.scheme in {"http", "https"} and (parsed.username or parsed.password):
                raise GitLabError("Repository URL must not embed credentials")
            host = parsed.hostname
            path = parsed.path
        elif "@" in raw and ":" in raw.split("@", 1)[1]:
            # SCP-like Git clone URL: git@gitlab.example:group/project.git
            _, host_and_path = raw.split("@", 1)
            host, path = host_and_path.split(":", 1)

        if host and self._host and host.lower() != self._host.lower():
            raise GitLabError("Repository host does not match configured GitLab")

        project_path = unquote(path).strip("/")
        if project_path.endswith(".git"):
            project_path = project_path[:-4]
        components = project_path.split("/")
        if not project_path or any(component in {"", ".", ".."} for component in components):
            raise GitLabError("Repository URL does not identify a GitLab project")
        return project_path


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


def _require_nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


# A concise alias is useful for callers that do not need to distinguish the
# implementation from the adapter boundary.
GitLabClient = GitLabHttpAdapter
