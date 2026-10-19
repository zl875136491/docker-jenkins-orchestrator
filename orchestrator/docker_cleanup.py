from __future__ import annotations

import time
from typing import Any

from orchestrator.build_resources import ResourceConflict
from orchestrator.docker_services import DockerServiceError, DockerSwarmAdapter
from orchestrator.models import ResourceState
from orchestrator.naming import build_namespace


BUILD_LABEL = "io.docker-jenkins-orchestrator.build-id"
APP_LABEL = "io.docker-jenkins-orchestrator.appid"
NAMESPACE_LABEL = "io.docker-jenkins-orchestrator.namespace"
MANAGED_LABEL = "io.docker-jenkins-orchestrator.managed"


class DockerBuildCleaner:
    def __init__(self, settings: Any, repository: Any, *, client: Any = None, client_factory: Any = None) -> None:
        self.settings = settings
        self.repository = repository
        self.client_factory = client_factory or DockerSwarmAdapter._create_client
        self.client = client or self.client_factory(settings.docker_base_url)
        info = self.client.info()
        if not (info.get("ID") or info.get("Swarm", {}).get("NodeID")):
            raise ResourceConflict("Docker cleanup target has no verifiable identity")
        self.target = "docker:" + str(info.get("ID") or info.get("Swarm", {}).get("NodeID"))
        self.node_id = info.get("Swarm", {}).get("NodeID")
        self.clients = {self.target: self.client}

    def target_client(self, target: str):
        if target in self.clients:
            return self.clients[target]
        endpoint = self.settings.cleanup_docker_targets.get(target)
        if endpoint is None:
            raise ResourceConflict("Resource execution target is unavailable; configure its controlled Docker endpoint")
        client = self.client_factory(endpoint)
        info = client.info()
        actual = "docker:" + str(info.get("ID") or info.get("Swarm", {}).get("NodeID"))
        if actual != target and not (target.startswith("docker-node:") and info.get("Swarm", {}).get("NodeID") == target.split(":", 1)[1]):
            raise ResourceConflict("Configured Docker endpoint identity does not match resource target")
        self.clients[target] = client
        return client

    def assert_owner(self, obj: Any, manifest: Any, resource: Any = None) -> None:
        attrs = obj.attrs
        labels = attrs.get("Spec", {}).get("Labels") or attrs.get("Labels") or attrs.get("Config", {}).get("Labels") or {}
        if labels.get(APP_LABEL) != manifest.appid or labels.get(BUILD_LABEL) != manifest.build_id or labels.get(MANAGED_LABEL) != "true" or labels.get(NAMESPACE_LABEL) != build_namespace(manifest.appid, manifest.build_id):
            raise ResourceConflict("Resource ownership could not be verified")
        identifier = getattr(obj, "id", None) or attrs.get("Id") or attrs.get("ID") or attrs.get("Name")
        if resource is not None and resource.native_id is not None and identifier != resource.native_id:
            raise ResourceConflict("Resource identity changed; refusing deletion")
        if resource is not None and resource.kind == "volume" and resource.metadata.get("creation_token") != DockerSwarmAdapter._volume_creation_token(obj):
            raise ResourceConflict("Volume creation identity changed; refusing deletion")

    def discover(self, manifest: Any, tracker: Any) -> None:
        runtime_targets = {resource.target for resource in manifest.resources.values()
                           if resource.kind in {"service", "network"} and resource.managed}
        if len(runtime_targets) > 1:
            raise ResourceConflict("Build runtime control target is inconsistent")
        if runtime_targets and self.target not in runtime_targets:
            target = next(iter(runtime_targets))
            self.client = self.target_client(target)
            self.target = target
            self.node_id = self.client.info().get("Swarm", {}).get("NodeID")
        for allocation in self.repository.list_build_ports(manifest.build_id):
            tracker.record("port", f"{allocation['port']}/{allocation['protocol']}", target=allocation["target"],
                           state=ResourceState.PRESENT, metadata={key: allocation[key] for key in ("port", "protocol", "service_name")})
        filters = {"label": [f"{BUILD_LABEL}={manifest.build_id}", f"{APP_LABEL}={manifest.appid}"]}
        for kind, collection in (("service", self.client.services), ("network", self.client.networks)):
            for obj in collection.list(filters=filters):
                self.assert_owner(obj, manifest)
                name = getattr(obj, "name", None) or obj.attrs.get("Spec", {}).get("Name") or obj.attrs.get("Name")
                tracker.record(kind, name, target=self.target, native_id=obj.id, state=ResourceState.PRESENT)
        targets = {self.target} | {resource.target for resource in manifest.resources.values() if resource.target.startswith(("docker:", "docker-node:"))}
        for target in targets:
            client = self.target_client(target)
            for volume in client.volumes.list(filters=filters):
                self.assert_owner(volume, manifest)
                tracker.record("volume", volume.name, target=target, native_id=volume.id, state=ResourceState.PRESENT,
                               metadata={"creation_token": DockerSwarmAdapter._volume_creation_token(volume)})
            for container in client.containers.list(all=True, filters=filters):
                self.assert_owner(container, manifest)
                tracker.record("container", container.name, target=target, native_id=container.id, state=ResourceState.PRESENT,
                               metadata={"service_id": container.attrs.get("Config", {}).get("Labels", {}).get("com.docker.swarm.service.id")})
        current = tracker.repository.get_build_resources(manifest.build_id)
        for resource in current.resources.values():
            if resource.kind != "service" or not resource.managed or resource.state == ResourceState.REMOVED:
                continue
            service = self._find(self.client.services, resource)
            if service is None:
                continue
            self.assert_owner(service, manifest, resource)
            for task in service.tasks():
                container_id = task.get("Status", {}).get("ContainerStatus", {}).get("ContainerID")
                node_id = task.get("NodeID")
                if container_id:
                    target = self.target if node_id in (None, self.node_id) else "docker-node:" + node_id
                    tracker.record("container", container_id, target=target, native_id=container_id, state=ResourceState.PRESENT,
                                   metadata={"service_id": service.id, "node_id": node_id})

    def remove(self, manifest: Any, resource: Any, tracker: Any) -> None:
        tracker.guard()
        client = self.target_client(resource.target)
        if resource.kind == "service":
            service = self._find(client.services, resource)
            if service is not None:
                self.assert_owner(service, manifest, resource)
                tracker.guard()
                service.remove()
            return
        if resource.kind == "container":
            container = self._find(client.containers, resource)
            if container is None:
                return
            self.assert_owner(container, manifest, resource)
            labels = container.attrs.get("Config", {}).get("Labels", {})
            service_id = labels.get("com.docker.swarm.service.id")
            if service_id != resource.metadata.get("service_id") or not service_id:
                raise ResourceConflict("Container service ownership is unavailable")
            if self._get(self.client.services, service_id) is not None:
                raise ResourceConflict("Container service is still present")
            if container.status in {"running", "restarting", "paused"}:
                raise ResourceConflict("Container has not stopped after service removal")
            tracker.guard()
            container.remove(force=False, v=False)
            return
        if resource.kind == "volume":
            volume = self._find(client.volumes, resource)
            if volume is None:
                return
            self.assert_owner(volume, manifest, resource)
            if client.containers.list(all=True, filters={"volume": resource.name}):
                raise ResourceConflict("Volume is still used by a container")
            for service in self.client.services.list():
                mounts = service.attrs.get("Spec", {}).get("TaskTemplate", {}).get("ContainerSpec", {}).get("Mounts", [])
                if any(mount.get("Type") == "volume" and mount.get("Source") == resource.name for mount in mounts):
                    raise ResourceConflict("Volume is still referenced by a service")
            tracker.guard()
            volume.remove(force=False)
            return
        if resource.kind == "network":
            network = self._find(client.networks, resource)
            if network is None:
                return
            self.assert_owner(network, manifest, resource)
            for service in client.services.list():
                attached = service.attrs.get("Spec", {}).get("TaskTemplate", {}).get("Networks", [])
                if any(item.get("Target") in {network.id, resource.name} for item in attached):
                    raise ResourceConflict("Network is still used by a service")
            for identifier in network.attrs.get("Containers") or {}:
                if self._get(client.containers, identifier) is not None:
                    raise ResourceConflict("Network still has container attachments")
            tracker.guard()
            network.remove()
            return
        if resource.kind == "local_image":
            image = self._get(client.images, resource.name)
            if image is None:
                return
            if resource.native_id and image.id != resource.native_id:
                raise ResourceConflict("Local image identity changed")
            if not resource.name.rsplit(":", 1)[-1].startswith(manifest.build_id + "-"):
                raise ResourceConflict("Local image reference is not build-exclusive")
            for container in client.containers.list(all=True):
                if container.attrs.get("Config", {}).get("Image", "").split("@", 1)[0] == resource.name:
                    raise ResourceConflict("Image reference is still used by a container")
            for service in self.client.services.list():
                reference = service.attrs.get("Spec", {}).get("TaskTemplate", {}).get("ContainerSpec", {}).get("Image", "")
                if reference.split("@", 1)[0] == resource.name:
                    raise ResourceConflict("Image reference is still used by a service")
            other_tags = [tag for tag in image.tags if tag != resource.name]
            if not other_tags:
                for service in self.client.services.list():
                    reference = service.attrs.get("Spec", {}).get("TaskTemplate", {}).get("ContainerSpec", {}).get("Image", "")
                    other_image = self._get(client.images, reference) if reference else None
                    if other_image is not None and other_image.id == image.id:
                        raise ResourceConflict("Image layers are still referenced by a service")
            if not other_tags and client.containers.list(all=True, filters={"ancestor": image.id}):
                raise ResourceConflict("Image layers are still used by a container")
            tracker.guard()
            client.images.remove(resource.name, force=False, noprune=True)
            return
        if resource.kind == "port":
            number = resource.metadata["port"]
            protocol = resource.metadata["protocol"]
            for service in client.services.list():
                ports = service.attrs.get("Endpoint", {}).get("Ports", []) + service.attrs.get("Spec", {}).get("EndpointSpec", {}).get("Ports", [])
                for port in ports:
                    if port.get("PublishedPort") == number and port.get("Protocol", "tcp") == protocol:
                        raise ResourceConflict("Published port is still occupied")
            for container in client.containers.list(all=True):
                if (number, protocol) in DockerSwarmAdapter._container_bound_ports(container):
                    raise ResourceConflict("Published port is still bound by a container")
            tracker.guard()
            if not self.repository.release_port(resource.target, protocol, number, manifest.build_id):
                raise ResourceConflict("Port reservation belongs to another build")
            return
        raise ResourceConflict("Unsupported Docker cleanup resource")

    def _find(self, collection: Any, resource: Any):
        obj = self._get(collection, resource.native_id or resource.name)
        if obj is None and resource.native_id and resource.native_id != resource.name:
            obj = self._get(collection, resource.name)
        if obj is not None and resource.native_id and obj.id != resource.native_id:
            raise ResourceConflict("Resource identity changed; refusing deletion")
        return obj

    def assert_directory_unused(self, path: str, tracker: Any) -> None:
        from pathlib import Path

        directory = Path(path)
        for client in self.clients.values():
            tracker.guard()
            for container in client.containers.list(all=True):
                for mount in container.attrs.get("Mounts", []):
                    source = mount.get("Source", "")
                    if mount.get("Type") == "bind" and source and (Path(source).is_relative_to(directory) or directory.is_relative_to(source)):
                        labels = container.attrs.get("Config", {}).get("Labels") or {}
                        host_root = self.settings.workspace_host_root or self.settings.workspace_root
                        manager = labels.get("io.docker-jenkins-orchestrator.workspace-manager")
                        if (
                            manager in {"api", "worker"}
                            and labels.get("com.docker.compose.service") == manager
                            and labels.get("io.docker-jenkins-orchestrator.worker-name") == self.settings.worker_name
                            and Path(source) == host_root.absolute()
                            and mount.get("Destination") == str(self.settings.workspace_root.absolute())
                        ):
                            continue
                        raise ResourceConflict("Build directory is still mounted by a container")
        for service in self.client.services.list():
            mounts = service.attrs.get("Spec", {}).get("TaskTemplate", {}).get("ContainerSpec", {}).get("Mounts", [])
            for mount in mounts:
                source = mount.get("Source", "")
                if mount.get("Type") == "bind" and source and (Path(source).is_relative_to(directory) or directory.is_relative_to(source)):
                    raise ResourceConflict("Build directory is still mounted by a service")

    @staticmethod
    def _get(collection: Any, identifier: str):
        try:
            return collection.get(identifier)
        except Exception as exc:
            if DockerSwarmAdapter._is_not_found(exc):
                return None
            raise DockerServiceError("Unable to inspect cleanup resource") from None

    def wait_containers_stopped(self, manifest: Any, tracker: Any) -> None:
        deadline = time.monotonic() + self.settings.cleanup_timeout_seconds
        while True:
            tracker.guard()
            running = []
            for resource in self.repository.get_build_resources(manifest.build_id).resources.values():
                if resource.kind != "container" or not resource.managed or resource.state == ResourceState.REMOVED:
                    continue
                client = self.target_client(resource.target)
                container = self._find(client.containers, resource)
                if container is not None and container.status in {"running", "restarting", "paused"}:
                    running.append(resource)
            if not running:
                return
            if time.monotonic() >= deadline:
                raise ResourceConflict("Build containers have not stopped within cleanup timeout")
            time.sleep(0.2)

    def verify_empty(self, manifest: Any, tracker: Any) -> None:
        collections = {"service": "services", "container": "containers", "network": "networks", "volume": "volumes", "local_image": "images"}
        for resource in manifest.resources.values():
            if resource.kind not in collections or not resource.managed or resource.state != ResourceState.REMOVED:
                continue
            tracker.guard()
            client = self.target_client(resource.target)
            try:
                collection = getattr(client, collections[resource.kind])
                obj = self._get(collection, resource.name) if resource.kind == "local_image" else self._find(collection, resource)
                if obj is not None:
                    raise ResourceConflict("Registered build resource still exists after cleanup")
            except Exception as exc:
                tracker.update(resource, ResourceState.ERROR, "Registered resource removal could not be verified")
                raise ResourceConflict("Registered resource removal could not be verified") from exc
        filters = {"label": [f"{BUILD_LABEL}={manifest.build_id}", f"{APP_LABEL}={manifest.appid}"]}
        for client in self.clients.values():
            tracker.guard()
            if client.containers.list(all=True, filters=filters) or client.volumes.list(filters=filters):
                raise ResourceConflict("Build still has containers or volumes after cleanup")
        if self.client.services.list(filters=filters) or self.client.networks.list(filters=filters):
            raise ResourceConflict("Build still has services or networks after cleanup")
