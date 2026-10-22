"""GitLab adapter used by application setup and the build pipeline.

The adapter confirms a repository/ref, fully clones the repository at the
resolved immutable commit, and discovers a conventional Compose file locally.
Jenkins performs its own checkout later for the application build.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

import yaml

from orchestrator.adapters import AdapterError


# Docker Compose's current spelling is preferred, while the docker-compose
# names remain common legacy conventions.  Keep this ordered: when a
# repository contains more than one root-level candidate, choose the same
# precedence users get from the Compose CLI rather than failing arbitrarily.
_COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yml",
    "docker-compose.yaml",
)


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
        max_bytes: int | None = None,
    ) -> HttpResponse: ...


class UrllibHttpTransport:
    """Small production transport; tests should inject an ``HttpTransport`` fake."""

    def __init__(self, timeout: float = 30, *, ca_file: str | None = None, persist_cookies: bool = True) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout
        # Jenkins' CSRF crumb issuer sets a session cookie that must accompany
        # the following build request. Keep it in this transport instance so
        # adapters share one authenticated HTTP session without exposing it to
        # callers or logs.
        self._opener = build_opener()
        if ca_file is not None:
            context = ssl.create_default_context(cafile=ca_file)
            context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
            self._opener = build_opener(HTTPSHandler(context=context))
        self._cookie_jar = CookieJar()
        if persist_cookies:
            self._opener.add_handler(HTTPCookieProcessor(self._cookie_jar))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        request = Request(url, data=body, headers=dict(headers), method=method.upper())
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read() if max_bytes is None else response.read(max_bytes),
                )
        except HTTPError as exc:
            # HTTP status failures are useful to the adapter and contain no
            # reason to surface a server body, which may contain sensitive data.
            try:
                return HttpResponse(
                    status_code=exc.code,
                    headers=dict(exc.headers.items()) if exc.headers else {},
                    body=exc.read() if max_bytes is None else exc.read(max_bytes),
                )
            finally:
                exc.close()
        except (OSError, TimeoutError, URLError, ValueError):
            raise TransportError("HTTP transport request failed") from None


class GitLabError(AdapterError):
    """A GitLab operation failed with a message safe for events and alerts."""


class GitLabNotFoundError(GitLabError):
    """GitLab did not find the requested project or revision."""


class GitLabComposeNotFoundError(GitLabNotFoundError):
    """The repository does not contain a conventional Docker Compose file."""


class GitLabComposeAmbiguousError(GitLabError):
    """More than one conventional Docker Compose file was found."""


class GitLabComposeInvalidError(GitLabError):
    """A discovered Docker Compose file is not valid for this application."""


class GitLabRepositoryCloneError(GitLabError):
    """Git could not clone and check out the resolved repository commit."""


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


@dataclass(frozen=True)
class GitLabComposeDocument:
    """A Docker Compose document read from one immutable repository commit."""

    path: str
    compose: Mapping[str, Any]
    commit_sha: str


class GitLabAdapter(Protocol):
    def validate_repository_ref(self, repository_url: str, git_ref: str) -> GitLabRepositoryRef: ...

    def discover_compose(self, ref: GitLabRepositoryRef) -> GitLabComposeDocument: ...

    def checkout_commit(self, ref: GitLabRepositoryRef, destination: str | Path) -> Path: ...


class GitLabHttpAdapter:
    """Validate repositories and refs through GitLab's v4 project API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: HttpTransport | None = None,
        timeout: float = 30,
        clone_timeout: float = 600,
    ) -> None:
        self._base_url = _normalise_base_url(base_url)
        if not isinstance(token, str) or not token.strip():
            raise ValueError("GitLab token must be configured")
        self._token = token
        self._transport = transport or UrllibHttpTransport(timeout)
        if clone_timeout <= 0:
            raise ValueError("clone_timeout must be positive")
        self._clone_timeout = clone_timeout
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
            clone_timeout=getattr(settings, "git_clone_timeout_seconds", 600),
        )

    def validate_repository_ref(self, repository_url: str, git_ref: str) -> GitLabRepositoryRef:
        """Confirm ``repository_url`` exists and resolve ``git_ref`` to a commit SHA."""

        project_path = self._project_path(repository_url)
        ref = _require_nonempty(git_ref, "git_ref")
        project = self._get_project(project_path)
        commit = self._get_commit(project.project_id, ref)
        commit_sha = commit.get("id")
        if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?", commit_sha):
            raise GitLabError("GitLab returned an invalid commit response")
        return GitLabRepositoryRef(
            project=project,
            git_ref=ref,
            commit_sha=commit_sha,
            repository_url=repository_url,
        )

    def discover_compose(self, ref: GitLabRepositoryRef) -> GitLabComposeDocument:
        """Fully clone the repository, then find and parse Compose locally.

        The GitLab API resolves a branch to an immutable SHA first. Git then
        performs a complete clone (no shallow/partial options), and the local
        checkout is pinned to that SHA before any Compose search is performed.
        """

        if not isinstance(ref, GitLabRepositoryRef):
            raise ValueError("ref must be a GitLabRepositoryRef")
        if not re.fullmatch(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?", ref.commit_sha):
            raise GitLabError("GitLab reference must contain a full commit SHA")

        with tempfile.TemporaryDirectory(prefix="orchestrator-gitlab-") as temporary_directory:
            repository_path = Path(temporary_directory) / "repository"
            self._clone_repository(ref, repository_path)
            compose_path = self._find_compose_path(repository_path)
            raw = (repository_path / compose_path).read_bytes()
        try:
            rendered = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise GitLabComposeInvalidError("Docker Compose file is not valid UTF-8") from None
        try:
            document = yaml.safe_load(rendered)
        except yaml.YAMLError:
            raise GitLabComposeInvalidError("Docker Compose file contains invalid YAML") from None
        if not isinstance(document, Mapping):
            raise GitLabComposeInvalidError("Docker Compose document must be an object")
        services = document.get("services")
        if not isinstance(services, Mapping) or not services:
            raise GitLabComposeInvalidError("Docker Compose document must contain a non-empty services object")
        return GitLabComposeDocument(path=compose_path, compose=document, commit_sha=ref.commit_sha)

    def checkout_commit(self, ref: GitLabRepositoryRef, destination: str | Path) -> Path:
        """Materialize a full repository checkout pinned to an already resolved SHA."""
        path = Path(destination).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._clone_repository(ref, path)
        return path

    def _clone_repository(self, ref: GitLabRepositoryRef, destination: Path) -> None:
        environment = os.environ.copy()
        # Do not let Git-specific variables from the API process redirect the
        # checkout or attach it to an existing/shallow object store. Normal
        # proxy, CA and user configuration remains available to Git.
        for name in (
            "GIT_DIR",
            "GIT_COMMON_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_SHALLOW_FILE",
            "GIT_NAMESPACE",
            "GIT_CONFIG_PARAMETERS",
        ):
            environment.pop(name, None)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_CONFIG_COUNT"] = "1"
        environment["GIT_CONFIG_KEY_0"] = "credential.helper"
        environment["GIT_CONFIG_VALUE_0"] = ""

        clone_url = self._clone_url(ref)
        parsed_url = urlsplit(clone_url)
        if parsed_url.scheme in {"http", "https"} and parsed_url.hostname:
            username_password = f"oauth2:{self._token}".encode("utf-8")
            authorization = base64.b64encode(username_password).decode("ascii")
            environment["GIT_CONFIG_COUNT"] = "2"
            environment["GIT_CONFIG_KEY_1"] = f"http.{parsed_url.scheme}://{parsed_url.netloc}/.extraheader"
            environment["GIT_CONFIG_VALUE_1"] = f"Authorization: Basic {authorization}"

        # This must remain a full clone. In particular, do not add --depth,
        # --single-branch, --filter, or replace it with GitLab tree/file APIs.
        self._run_git(
            ["git", "clone", "--quiet", "--no-checkout", "--", clone_url, str(destination)],
            environment,
        )
        self._verify_full_clone(destination, environment)

        try:
            has_commit = subprocess.run(
                ["git", "-C", str(destination), "cat-file", "-e", f"{ref.commit_sha}^{{commit}}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self._clone_timeout,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise GitLabRepositoryCloneError("Unable to clone GitLab repository") from None
        if has_commit.returncode != 0:
            self._run_git(
                ["git", "-C", str(destination), "fetch", "--quiet", "origin", ref.commit_sha],
                environment,
            )
            self._verify_full_clone(destination, environment)

        self._run_git(
            ["git", "-C", str(destination), "checkout", "--quiet", "--detach", ref.commit_sha],
            environment,
        )
        actual_sha = self._run_git(
            ["git", "-C", str(destination), "rev-parse", "HEAD"],
            environment,
        ).stdout.strip()
        if actual_sha.lower() != ref.commit_sha.lower():
            raise GitLabRepositoryCloneError("Git clone did not check out the resolved GitLab commit")

    def _verify_full_clone(self, repository_path: Path, environment: Mapping[str, str]) -> None:
        """Reject incomplete clone results before reading any Compose file."""

        shallow = self._run_git(
            ["git", "-C", str(repository_path), "rev-parse", "--is-shallow-repository"],
            environment,
        ).stdout.strip()
        if shallow != "false":
            raise GitLabRepositoryCloneError("A full clone is required; Git returned a shallow repository")

        configuration = self._run_git(
            ["git", "-C", str(repository_path), "config", "--local", "--list"],
            environment,
        ).stdout
        for setting in configuration.splitlines():
            key, _, value = setting.partition("=")
            key = key.lower()
            if key in {"extensions.partialclone", "remote.origin.partialclonefilter"} or (
                key == "remote.origin.promisor" and value.lower() not in {"false", "no", "off", "0"}
            ):
                raise GitLabRepositoryCloneError("A full clone is required; Git returned a partial repository")

    def _clone_url(self, ref: GitLabRepositoryRef) -> str:
        """Build a clone URL from GitLab metadata; never embed the access token."""

        web_url = ref.project.web_url
        if web_url:
            try:
                parsed = urlsplit(web_url)
                configured = urlsplit(self._base_url)
                project_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                configured_port = configured.port or (443 if configured.scheme == "https" else 80)
            except ValueError:
                raise GitLabError("GitLab returned an invalid project URL") from None
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.hostname.lower() != (self._host or "").lower()
                or parsed.scheme != configured.scheme
                or project_port != configured_port
            ):
                raise GitLabError("GitLab returned an invalid project URL")
            clone_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        else:
            base = self._base_url.removesuffix("/api/v4").rstrip("/")
            project_path = quote(ref.project.path_with_namespace.strip("/"), safe="/")
            clone_url = f"{base}/{project_path}"

        if not clone_url.endswith(".git"):
            clone_url += ".git"
        return clone_url

    def _run_git(self, command: list[str], environment: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self._clone_timeout,
                env=dict(environment),
                check=True,
            )
        except FileNotFoundError:
            raise GitLabRepositoryCloneError(
                "Git is required for full repository cloning; the runtime must include Git and CA certificates"
            ) from None
        except subprocess.TimeoutExpired:
            raise GitLabRepositoryCloneError("Git repository cloning timed out") from None
        except (OSError, subprocess.SubprocessError):
            # Git stderr may include the private repository URL or auth details.
            raise GitLabRepositoryCloneError("Unable to clone GitLab repository") from None

    def _find_compose_path(self, repository_path: Path) -> str:
        files: set[str] = set()

        def scan_error(_: OSError) -> None:
            raise GitLabRepositoryCloneError("Unable to scan the cloned GitLab repository") from None

        for directory, directories, filenames in os.walk(repository_path, onerror=scan_error, followlinks=False):
            # A complete clone contains its full object database. Prune .git
            # before traversing, and never follow repository symlinks outside
            # the checked-out worktree.
            directories[:] = [
                name for name in directories if name != ".git" and not (Path(directory) / name).is_symlink()
            ]
            for filename in filenames:
                if filename not in _COMPOSE_FILENAMES:
                    continue
                candidate = Path(directory) / filename
                if not candidate.is_symlink() and candidate.is_file():
                    files.add(candidate.relative_to(repository_path).as_posix())
        root_files = {path for path in files if "/" not in path}
        for filename in _COMPOSE_FILENAMES:
            if filename in root_files:
                return filename
        nested_files = sorted(files)
        if not nested_files:
            raise GitLabComposeNotFoundError(
                "No conventional Docker Compose file was found in the repository"
            )
        if len(nested_files) > 1:
            raise GitLabComposeAmbiguousError(
                "Multiple Docker Compose files were found in the repository"
            )
        return nested_files[0]

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
        payload = self._request_json_payload(path, not_found_message=not_found_message)
        if not isinstance(payload, Mapping):
            raise GitLabError("GitLab returned an invalid JSON response")
        return payload

    def _request_json_payload(self, path: str, *, not_found_message: str) -> Any:
        response = self._request_response(path, not_found_message=not_found_message)
        try:
            return response.json()
        except ValueError:
            raise GitLabError("GitLab returned an invalid JSON response") from None

    def _request_response(self, path: str, *, not_found_message: str) -> HttpResponse:
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
        return response

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
