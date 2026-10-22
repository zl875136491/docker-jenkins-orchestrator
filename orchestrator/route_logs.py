"""Read-only, build-scoped Swarm stdout/stderr collection for Debug Agents."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any, Callable

from orchestrator.build_resources import ResourceConflict
from orchestrator.docker_cleanup import DockerBuildCleaner
from orchestrator.docker_services import DockerSwarmAdapter
from orchestrator.models import ResourceState


class BuildLogCollector:
    """Collect at most 100 lines/service, 200 lines and 64 KiB in total.

    Only tracked, managed Swarm services with both a matching immutable native
    ID and the app/build/namespace ownership labels are eligible.  In particular
    this never invokes cleanup discovery (which writes the resource tracker),
    enumerates unrelated services, or connects directly to task containers on
    other Swarm nodes.  ``Service.logs`` is the manager-side aggregated stream.
    """

    def __init__(self, settings: Any, repository: Any, *, client_factory: Callable[..., Any] | None = None) -> None:
        self.settings = settings
        self.repository = repository
        self.client_factory = client_factory

    def collect(self, build_id: str) -> dict[str, str]:
        try:
            manifest = self.repository.get_build_resources(build_id)
            if manifest is None or manifest.build_id != build_id:
                return {"_collector": "Build resource manifest is unavailable; no logs were read"}
            get_build = getattr(self.repository, "get_build", None)
            if get_build is not None:
                build = get_build(build_id)
                if build is None or build.appid != manifest.appid:
                    return {"_collector": "Build ownership could not be verified; no logs were read"}
            resources = [
                resource for resource in manifest.resources.values()
                if resource.kind == "service" and resource.managed and resource.state != ResourceState.REMOVED
            ]
        except Exception:
            return {"_collector": "Build resource manifest could not be read"}
        if not resources:
            return {"_collector": "No managed Swarm services are recorded for this build"}
        clients: dict[int, Any] = {}
        factory = self.client_factory or DockerSwarmAdapter._create_client

        def create_client(endpoint: str | None) -> Any:
            client = factory(endpoint)
            clients[id(client)] = client
            return client

        try:
            cleaner = DockerBuildCleaner(self.settings, self.repository, client_factory=create_client)
        except Exception:
            self._close_clients(clients)
            return {"_collector": "Controlled Docker log endpoint is unavailable"}
        output: dict[str, str] = {}
        bytes_remaining = 65536
        lines_remaining = 200
        try:
            for resource in resources:
                if bytes_remaining <= 0 or lines_remaining <= 0:
                    break
                key = resource.name
                if key in output:
                    key = f"{resource.name}@{resource.target}"
                try:
                    if not resource.native_id:
                        raise ResourceConflict("Service has no recorded immutable identity")
                    client = cleaner.target_client(resource.target)
                    service = cleaner._find(client.services, resource)
                    if service is None:
                        text = "Tracked Swarm service is no longer present"
                    else:
                        cleaner.assert_owner(service, manifest, resource)
                        stream = service.logs(stdout=True, stderr=True, follow=False, tail=min(100, lines_remaining), timestamps=True)
                        text = self._read_tail(stream, min(100, lines_remaining), bytes_remaining)
                        if not text:
                            text = "Swarm service has not emitted stdout/stderr logs"
                except ResourceConflict:
                    text = "Swarm service identity or build ownership could not be verified; no logs were read"
                except Exception:
                    # Driver errors can include daemon addresses or credentials.
                    text = "Swarm stdout/stderr logs could not be read from the controlled Docker endpoint"
                # Replacement characters can expand a partial UTF-8 boundary;
                # preserve the newest end of the log when enforcing the cap.
                encoded = text.encode("utf-8", errors="replace")[-bytes_remaining:]
                text = encoded.decode("utf-8", errors="ignore")
                lines = text.splitlines(keepends=True)[:lines_remaining]
                text = "".join(lines)
                output[key] = text
                bytes_remaining -= len(text.encode("utf-8"))
                lines_remaining -= len(lines)
            return output
        finally:
            # Include connections rejected by an identity check, which are not
            # inserted into DockerBuildCleaner's verified-client cache.
            self._close_clients(clients)

    @staticmethod
    def _close_clients(clients: Mapping[int, Any]) -> None:
        for client in clients.values():
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    def _read_tail(self, stream: Any, max_lines: int, max_bytes: int) -> str:
        if isinstance(stream, (bytes, str)):
            chunks = iter([stream])
        elif isinstance(stream, Mapping):
            raise ValueError("Unexpected Docker log stream")
        else:
            chunks = iter(stream)
        tail = bytearray()
        deadline = time.monotonic() + float(getattr(self.settings, "external_request_timeout_seconds", 30))
        scanned_bytes = 0
        count = 0
        try:
            for chunk in chunks:
                count += 1
                if not isinstance(chunk, (bytes, str)):
                    raise ValueError("Unexpected Docker log data")
                data = chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else chunk
                scanned_bytes += len(data)
                # Docker tail is per task; bound work even with many replicas or
                # a broken/hostile daemon ignoring follow=False.
                if count > 4096 or scanned_bytes > 4 * 1024 * 1024 or time.monotonic() > deadline:
                    raise ValueError("Docker log scan exceeded its bound")
                tail.extend(data[-max_bytes:])
                if len(tail) > max_bytes:
                    del tail[:-max_bytes]
            text = bytes(tail).decode("utf-8", errors="replace")
            return "".join(text.splitlines(keepends=True)[-max_lines:])
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
