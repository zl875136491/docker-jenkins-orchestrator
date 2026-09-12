"""Docker SDK adapter for mirroring pinned base images into Harbor.

The module deliberately accepts a Docker client instance.  That keeps the
adapter straightforward to exercise with a fake client and prevents creating a
daemon connection until a synchronisation is requested in production.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from orchestrator.adapters import AdapterError
from orchestrator.models import BaseImage, utc_now


_DIGEST_PATTERN = re.compile(r"^[A-Za-z0-9_+.-]+:[A-Fa-f0-9]{6,}$")
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class RegistrySyncError(AdapterError):
    """A base-image mirroring operation failed without exposing credentials."""


@dataclass(frozen=True)
class ImageReference:
    """The parsed pieces needed by Docker's ``tag`` and ``push`` APIs."""

    repository: str
    tag: str

    @property
    def value(self) -> str:
        return f"{self.repository}:{self.tag}"


class DockerRegistrySynchronizer:
    """Mirror stable source images into a Harbor project using the Docker SDK.

    ``docker_client`` is expected to be a high-level ``docker.DockerClient``.
    Supplying one is useful both for dependency injection and for sharing a
    process-wide client.  When it is omitted, the Docker SDK import and client
    construction happen lazily on the first call to :meth:`sync`.
    """

    def __init__(
        self,
        harbor_url: str,
        username: str | None = None,
        password: str | None = None,
        project: str = "boot-images",
        *,
        docker_client: Any | None = None,
        client_factory: Callable[[str | None], Any] | None = None,
    ) -> None:
        self.registry = self._registry_host(harbor_url)
        self.username = username
        self._password = password
        self.project = self._project_name(project)
        self._docker_client = docker_client
        self._client_factory = client_factory or self._create_client

        if (username is None) != (password is None):
            raise ValueError("Harbor credentials require both a username and password")

    @classmethod
    def from_settings(cls, settings: Any, *, docker_client: Any | None = None) -> "DockerRegistrySynchronizer":
        """Create an adapter from the project's settings object.

        Keeping this convenience constructor here avoids importing settings in
        the adapter itself and leaves callers free to supply a fake client.
        """

        if not settings.harbor_url:
            raise ValueError("Harbor URL is required for base-image synchronization")
        return cls(
            settings.harbor_url,
            settings.harbor_user,
            settings.harbor_password,
            settings.harbor_boot_images_project,
            docker_client=docker_client,
        )

    def __repr__(self) -> str:
        username = self.username if self.username is not None else None
        return (
            "DockerRegistrySynchronizer("
            f"registry={self.registry!r}, username={username!r}, password='***', project={self.project!r})"
        )

    def harbor_reference(self, source_image: str) -> str:
        """Return the fully-qualified Harbor reference for a stable source image."""

        source = self._parse_stable_reference(source_image)
        path = self._mirror_path(source.repository)
        return ImageReference(f"{self.registry}/{self.project}/{path}", source.tag).value

    def sync(self, source_image: str) -> BaseImage:
        """Pull, tag, and push one source image, returning its persisted shape.

        A successful Docker push does not always return a manifest digest (for
        example with older daemons), so ``digest`` is intentionally optional.
        Errors are deliberately broad and credential-free for use in events and
        alerts.
        """

        source = self._parse_stable_reference(source_image)
        target = ImageReference(
            f"{self.registry}/{self.project}/{self._mirror_path(source.repository)}",
            source.tag,
        )

        try:
            client = self._client()
            self._login(client)
            image = client.images.pull(source_image)
            tagged = image.tag(target.repository, tag=target.tag)
            if tagged is False:
                raise RegistrySyncError("Docker could not tag the base image")
            digest = self._push(client, target)
            if digest is None:
                digest = self._image_digest(image, target.repository)
            return BaseImage(
                source_image=source_image,
                harbor_reference=target.value,
                digest=digest,
                status="synced",
                synced_at=utc_now(),
            )
        except RegistrySyncError:
            raise
        except Exception as exc:
            raise RegistrySyncError("Unable to synchronize base image with Harbor") from exc

    # A verb that reads naturally at Celery-task call sites.
    synchronize = sync
    sync_base_image = sync

    def push_base_image(self, source_image: str) -> str:
        """Compatibility shim for the original string-only Harbor protocol."""

        return self.sync(source_image).harbor_reference

    def sync_many(self, source_images: Iterable[str]) -> list[BaseImage]:
        """Synchronize all images, returning a record for every input.

        Per-image failures are represented as failed ``BaseImage`` records so a
        caller can save every outcome without losing the rest of the batch.
        """

        results: list[BaseImage] = []
        for source_image in source_images:
            try:
                results.append(self.sync(source_image))
            except RegistrySyncError as exc:
                try:
                    reference = self.harbor_reference(source_image)
                except RegistrySyncError:
                    # Preserve the source as the only usable identity when it
                    # was malformed; it is not a registry URL or credential.
                    reference = ""
                results.append(
                    BaseImage(
                        source_image=source_image,
                        harbor_reference=reference,
                        status="failed",
                        error=str(exc),
                        synced_at=utc_now(),
                    )
                )
        return results

    def _client(self) -> Any:
        if self._docker_client is None:
            self._docker_client = self._client_factory(None)
        return self._docker_client

    @staticmethod
    def _create_client(base_url: str | None) -> Any:
        try:
            import docker
        except ImportError as exc:
            raise RegistrySyncError("Docker SDK is required for base-image synchronization") from exc
        if base_url:
            return docker.DockerClient(base_url=base_url)
        return docker.from_env()

    def _login(self, client: Any) -> None:
        if self.username is None:
            return
        # Docker uses the registry host (not its https URL) for login.
        client.login(username=self.username, password=self._password, registry=self.registry)

    def _push(self, client: Any, target: ImageReference) -> str | None:
        response = client.images.push(target.repository, tag=target.tag, stream=True, decode=True)
        digest: str | None = None
        for event in self._push_events(response):
            error = self._event_error(event)
            if error:
                raise RegistrySyncError("Harbor rejected the base image push")
            candidate = self._event_digest(event)
            if candidate is not None:
                digest = candidate
        return digest

    @classmethod
    def _push_events(cls, response: Any) -> Iterable[Mapping[str, Any]]:
        if response is None:
            return ()
        if isinstance(response, Mapping):
            return (response,)
        if isinstance(response, bytes):
            response = response.decode("utf-8", errors="replace")
        if isinstance(response, str):
            events: list[Mapping[str, Any]] = []
            for line in response.splitlines():
                item = line.strip()
                if not item:
                    continue
                try:
                    decoded = json.loads(item)
                except json.JSONDecodeError:
                    if _DIGEST_PATTERN.fullmatch(item):
                        events.append({"digest": item})
                    continue
                if isinstance(decoded, Mapping):
                    events.append(decoded)
            return tuple(events)
        if isinstance(response, Iterable):
            return tuple(item for item in response if isinstance(item, Mapping))
        return ()

    @staticmethod
    def _event_error(event: Mapping[str, Any]) -> bool:
        if event.get("error"):
            return True
        detail = event.get("errorDetail")
        return isinstance(detail, Mapping) and bool(detail.get("message"))

    @staticmethod
    def _event_digest(event: Mapping[str, Any]) -> str | None:
        candidates = (event.get("digest"), event.get("Digest"))
        auxiliary = event.get("aux")
        if isinstance(auxiliary, Mapping):
            candidates += (auxiliary.get("Digest"), auxiliary.get("digest"))
        for candidate in candidates:
            if isinstance(candidate, str) and _DIGEST_PATTERN.fullmatch(candidate):
                return candidate
        return None

    @staticmethod
    def _image_digest(image: Any, target_repository: str) -> str | None:
        attributes = getattr(image, "attrs", {})
        if not isinstance(attributes, Mapping):
            return None
        repo_digests = attributes.get("RepoDigests", [])
        if not isinstance(repo_digests, list):
            return None
        for item in repo_digests:
            if not isinstance(item, str) or "@" not in item:
                continue
            repository, digest = item.rsplit("@", 1)
            if repository == target_repository and _DIGEST_PATTERN.fullmatch(digest):
                return digest
        return None

    @staticmethod
    def _registry_host(harbor_url: str) -> str:
        if not isinstance(harbor_url, str) or not harbor_url.strip():
            raise ValueError("Harbor URL must be a non-empty URL or registry host")
        value = harbor_url.strip().rstrip("/")
        parsed = urlparse(value if "://" in value else f"//{value}")
        if not parsed.netloc or parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("Harbor URL must identify a registry host without a path")
        if parsed.username or parsed.password:
            raise ValueError("Harbor URL must not contain credentials")
        return parsed.netloc

    @staticmethod
    def _project_name(project: str) -> str:
        if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", project):
            raise ValueError("Harbor project name is invalid")
        return project

    @classmethod
    def _parse_stable_reference(cls, source_image: str) -> ImageReference:
        if not isinstance(source_image, str) or not source_image or source_image != source_image.strip():
            raise RegistrySyncError("Base image reference must be a non-empty string")
        if "@" in source_image:
            # A digest can appear alongside a stable tag, but a digest-only
            # reference cannot be mirrored as a tag without inventing one.
            image_name, digest = source_image.rsplit("@", 1)
            if not _DIGEST_PATTERN.fullmatch(digest):
                raise RegistrySyncError("Base image digest is invalid")
        else:
            image_name = source_image
        if any(character.isspace() for character in image_name) or "://" in image_name:
            raise RegistrySyncError("Base image reference is invalid")

        slash = image_name.rfind("/")
        colon = image_name.rfind(":")
        if colon <= slash:
            raise RegistrySyncError("Base image must use an explicit stable tag")
        repository, tag = image_name[:colon], image_name[colon + 1 :]
        if not repository or not _TAG_PATTERN.fullmatch(tag) or tag.lower() == "latest":
            raise RegistrySyncError("Base image must use an explicit stable tag")
        return ImageReference(repository=repository, tag=tag)

    @staticmethod
    def _mirror_path(repository: str) -> str:
        """Retain source identity while keeping Docker Hub defaults concise."""

        if repository.startswith("docker.io/library/"):
            return repository.removeprefix("docker.io/library/")
        if repository.startswith("docker.io/"):
            return repository.removeprefix("docker.io/")
        return repository


# The shorter name is convenient for task code and preserves a clear Harbor
# domain term for callers that do not care which Docker client is underneath.
HarborRegistrySynchronizer = DockerRegistrySynchronizer
HarborRegistryAdapter = DockerRegistrySynchronizer
DockerRegistryAdapter = DockerRegistrySynchronizer
