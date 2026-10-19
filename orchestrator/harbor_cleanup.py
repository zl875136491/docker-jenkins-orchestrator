from __future__ import annotations

from base64 import b64encode
from typing import Any
from urllib.parse import quote, urlsplit

from orchestrator.adapters import AdapterError
from orchestrator.gitlab import UrllibHttpTransport


class HarborCleanupError(AdapterError):
    pass


class HarborImageCleaner:
    def __init__(self, settings: Any, *, transport: Any = None, guard: Any = None) -> None:
        raw = settings.harbor_url or ""
        self.base_url = raw.rstrip("/") if "://" in raw else "https://" + raw.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"}:
            raise HarborCleanupError("Harbor cleanup endpoint is invalid")
        self.registry = parsed.netloc
        self.project = settings.harbor_project
        self.transport = transport
        if self.transport is None:
            ca_file = getattr(settings, "harbor_ca_file", None)
            try:
                self.transport = UrllibHttpTransport(
                    settings.external_request_timeout_seconds,
                    ca_file=str(ca_file) if ca_file is not None else None,
                    persist_cookies=False,
                )
            except (OSError, ValueError):
                raise HarborCleanupError("Harbor certificate trust configuration is invalid") from None
        self.guard = guard
        credentials = f"{settings.harbor_user or ''}:{settings.harbor_password or ''}".encode()
        self.headers = {"Authorization": "Basic " + b64encode(credentials).decode(), "Accept": "application/json"}

    def remove_build_tag(self, reference: str, expected_repository: str, build_id: str, digest: str | None = None) -> str:
        if not reference.startswith(expected_repository + ":" + build_id + "-"):
            raise HarborCleanupError("Registry image ownership is not build-exclusive")
        repository, tag = reference.rsplit(":", 1)
        prefix = self.registry + "/" + self.project + "/"
        if not repository.startswith(prefix):
            raise HarborCleanupError("Registry image is outside the configured build project")
        name = repository[len(prefix):]
        path = f"/api/v2.0/projects/{quote(self.project, safe='')}/repositories/{quote(quote(name, safe=''), safe='')}/artifacts/{quote(tag, safe='')}"
        response = self._request("GET", path + "?with_tag=true")
        if response.status_code == 404:
            return "waiting_gc"
        if response.status_code != 200:
            raise HarborCleanupError("Unable to verify Harbor build image")
        artifact = response.json()
        if digest and artifact.get("digest") != digest:
            raise HarborCleanupError("Registry image digest changed; refusing tag deletion")
        actual_digest = artifact.get("digest")
        if not isinstance(actual_digest, str) or not actual_digest.startswith("sha256:"):
            raise HarborCleanupError("Registry image digest is unavailable")
        shared = any(item.get("name") != tag for item in artifact.get("tags") or [])
        artifact_path = path.rsplit("/", 1)[0] + "/" + quote(actual_digest, safe="")
        if self.guard is not None:
            self.guard()
        removed = self._request("DELETE", artifact_path + "/tags/" + quote(tag, safe=""))
        if removed.status_code not in {200, 202, 204, 404}:
            raise HarborCleanupError("Unable to delete Harbor build tag")
        verified = self._request("GET", path)
        if verified.status_code != 404:
            raise HarborCleanupError("Harbor build tag removal could not be verified")
        return "shared_retained" if shared else "waiting_gc"

    def _request(self, method: str, path: str):
        try:
            return self.transport.request(method, self.base_url + path, headers=self.headers)
        except Exception:
            raise HarborCleanupError("Harbor cleanup request failed") from None
