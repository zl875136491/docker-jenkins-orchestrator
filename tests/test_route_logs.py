from copy import deepcopy
from types import SimpleNamespace

from docker.errors import NotFound
import pytest

from orchestrator.config import Settings
from orchestrator.docker_cleanup import APP_LABEL, BUILD_LABEL, MANAGED_LABEL, NAMESPACE_LABEL
from orchestrator.models import BuildResource, BuildResources, ResourceState
from orchestrator.naming import build_namespace
from orchestrator.route_logs import BuildLogCollector


APP_ID = "route-logs"
BUILD_ID = "a" * 32


class LogStream:
    def __init__(self, *chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


class Service:
    def __init__(self, name="build-web", native_id="service-1", *, labels=None, stream=None):
        self.name = name
        self.id = native_id
        self.attrs = {"Spec": {"Labels": {
            APP_LABEL: APP_ID, BUILD_LABEL: BUILD_ID, MANAGED_LABEL: "true",
            NAMESPACE_LABEL: build_namespace(APP_ID, BUILD_ID), **(labels or {}),
        }, "TaskTemplate": {"ContainerSpec": {}}}}
        self.stream = stream if stream is not None else LogStream(b"latest stdout\n", b"latest stderr\n")
        self.log_calls = []

    def logs(self, **kwargs):
        self.log_calls.append(kwargs)
        if isinstance(self.stream, Exception):
            raise self.stream
        return self.stream


class Services:
    def __init__(self, *services):
        self.services = {key: service for service in services for key in (service.id, service.name)}
        self.get_calls = []

    def get(self, identifier):
        self.get_calls.append(identifier)
        try:
            return self.services[identifier]
        except KeyError:
            raise NotFound("not found") from None

    def list(self, *_args, **_kwargs):
        pytest.fail("Log collection must not discover or enumerate other services")


class DockerClient:
    def __init__(self, *services, identity="engine-one"):
        self.identity = identity
        self.services = Services(*services)
        self.closed = False

    def info(self):
        return {"ID": self.identity, "Swarm": {"NodeID": "node-" + self.identity}}

    @property
    def containers(self):
        pytest.fail("Swarm log collection must not connect directly to task containers")

    @property
    def networks(self):
        pytest.fail("Read-only log collection must not discover or delete networks")

    def close(self):
        self.closed = True


class Repository:
    def __init__(self, *resources):
        self.manifest = BuildResources(build_id=BUILD_ID, appid=APP_ID, resources={resource.key: resource for resource in resources})
        self.build = SimpleNamespace(appid=APP_ID)

    def get_build_resources(self, build_id):
        assert build_id == BUILD_ID
        return self.manifest

    def get_build(self, build_id):
        assert build_id == BUILD_ID
        return self.build

    def put_build_resource(self, *_args, **_kwargs):
        pytest.fail("Log collection must not mutate build resource ownership")


def resource(**updates):
    return BuildResource(**{
        "key": "one", "name": "build-web", "native_id": "service-1", "kind": "service",
        "target": "docker:engine-one", "state": ResourceState.PRESENT, **updates,
    })


def collector(repository, *services, identity="engine-one", **settings):
    client = DockerClient(*services, identity=identity)
    calls = []

    def client_factory(endpoint):
        calls.append(endpoint)
        return client

    return BuildLogCollector(Settings(docker_base_url="tcp://manager:2375", **settings), repository, client_factory=client_factory), client, calls


def test_only_build_owned_swarm_stdout_stderr_are_collected_without_mutation():
    service = Service()
    repo = Repository(resource())
    original = deepcopy(repo.manifest.model_dump())
    provider, client, calls = collector(repo, service, Service("unrelated-service", "other-id", labels={BUILD_LABEL: "b" * 32}))
    result = provider.collect(BUILD_ID)
    assert result == {"build-web": "latest stdout\nlatest stderr\n"}
    assert service.log_calls == [{"stdout": True, "stderr": True, "follow": False, "tail": 100, "timestamps": True}]
    assert client.services.get_calls == ["service-1"]
    assert calls == ["tcp://manager:2375"]
    assert repo.manifest.model_dump() == original
    assert client.closed is True
    assert service.stream.closed is True


@pytest.mark.parametrize("labels", [
    {APP_LABEL: "another-app"}, {BUILD_LABEL: "b" * 32}, {MANAGED_LABEL: "false"},
    {NAMESPACE_LABEL: "other-namespace"}, {MANAGED_LABEL: None},
])
def test_ownership_label_mismatches_never_read_foreign_logs(labels):
    service = Service(labels=labels)
    provider, _client, _calls = collector(Repository(resource()), service)
    result = provider.collect(BUILD_ID)
    assert "ownership could not be verified" in result["build-web"]
    assert service.log_calls == []


def test_reused_service_name_cannot_bypass_the_recorded_native_id():
    replacement = Service(native_id="replacement-id")
    provider, client, _calls = collector(Repository(resource()), replacement)
    result = provider.collect(BUILD_ID)
    assert "identity or build ownership" in result["build-web"]
    assert client.services.get_calls == ["service-1", "build-web"]
    assert replacement.log_calls == []


def test_missing_native_identity_fails_closed_even_if_labels_could_match():
    service = Service()
    provider, client, _calls = collector(Repository(resource(native_id=None)), service)
    assert "identity or build ownership" in provider.collect(BUILD_ID)["build-web"]
    assert client.services.get_calls == []
    assert service.log_calls == []


@pytest.mark.parametrize("changes", [{"managed": False}, {"state": ResourceState.REMOVED}, {"kind": "container"}, {"kind": "network"}])
def test_unmanaged_removed_or_non_service_resources_are_not_read(changes):
    service = Service()
    provider, client, calls = collector(Repository(resource(**changes)), service)
    assert "No managed Swarm services" in provider.collect(BUILD_ID)["_collector"]
    assert calls == []
    assert client.services.get_calls == []
    assert service.log_calls == []


def test_controlled_execution_target_identity_is_verified_before_reading_logs():
    remote = DockerClient(Service(), identity="engine-two")
    local = DockerClient(identity="engine-one")
    calls = []

    def client_factory(endpoint):
        calls.append(endpoint)
        return remote if endpoint == "tcp://secondary:2375" else local

    repo = Repository(resource(target="docker:engine-two"))
    settings = Settings(docker_base_url="tcp://manager:2375", cleanup_docker_targets={"docker:engine-two": "tcp://secondary:2375"})
    result = BuildLogCollector(settings, repo, client_factory=client_factory).collect(BUILD_ID)
    assert result["build-web"].startswith("latest stdout")
    assert calls == ["tcp://manager:2375", "tcp://secondary:2375"]
    assert local.services.get_calls == []
    assert remote.services.get_calls == ["service-1"]
    assert local.closed and remote.closed


def test_wrong_controlled_execution_identity_does_not_read_a_service():
    client = DockerClient(Service(), identity="engine-one")
    repo = Repository(resource(target="docker:engine-two"))
    settings = Settings(cleanup_docker_targets={"docker:engine-two": "tcp://controlled:2375"})
    result = BuildLogCollector(settings, repo, client_factory=lambda _endpoint: client).collect(BUILD_ID)
    assert "identity or build ownership" in result["build-web"]
    assert client.services.get_calls == []


def test_rejected_remote_identity_and_failed_initial_inspection_close_connections():
    local = DockerClient(identity="engine-one")
    wrong_remote = DockerClient(identity="wrong-engine")
    settings = Settings(docker_base_url="tcp://manager:2375", cleanup_docker_targets={"docker:engine-two": "tcp://remote:2375"})
    repo = Repository(resource(target="docker:engine-two"))
    provider = BuildLogCollector(settings, repo, client_factory=lambda endpoint: local if endpoint == "tcp://manager:2375" else wrong_remote)
    assert "ownership could not be verified" in provider.collect(BUILD_ID)["build-web"]
    assert local.closed and wrong_remote.closed
    assert wrong_remote.services.get_calls == []

    class BrokenDockerClient(DockerClient):
        def info(self):
            raise RuntimeError("tcp://private-password@daemon")

    broken = BrokenDockerClient()
    result = BuildLogCollector(Settings(), Repository(resource()), client_factory=lambda _endpoint: broken).collect(BUILD_ID)
    assert result == {"_collector": "Controlled Docker log endpoint is unavailable"}
    assert broken.closed


def test_unconfigured_remote_target_never_connects_to_an_uncontrolled_daemon():
    provider, client, calls = collector(Repository(resource(target="docker:unconfigured")), Service())
    assert "identity or build ownership" in provider.collect(BUILD_ID)["build-web"]
    assert calls == ["tcp://manager:2375"]
    assert client.services.get_calls == []


def test_missing_service_reports_diagnostic_without_listing_other_builds():
    provider, client, _calls = collector(Repository(resource()))
    assert provider.collect(BUILD_ID) == {"build-web": "Tracked Swarm service is no longer present"}
    assert client.services.get_calls == ["service-1", "build-web"]


def test_driver_or_daemon_exception_does_not_leak_endpoint_credentials():
    service = Service(stream=RuntimeError("tcp://user:private-password@docker; private-api-token"))
    provider, _client, _calls = collector(Repository(resource()), service)
    result = provider.collect(BUILD_ID)
    assert "could not be read" in result["build-web"]
    assert "private" not in str(result)


def test_missing_manifest_and_build_identity_mismatch_fail_before_docker_access():
    repo = Repository(resource())
    provider, _client, calls = collector(repo, Service())
    repo.manifest = None
    assert "unavailable" in provider.collect(BUILD_ID)["_collector"]
    assert calls == []
    repo.manifest = BuildResources(build_id=BUILD_ID, appid=APP_ID)
    repo.build = SimpleNamespace(appid="other-app")
    assert "ownership could not be verified" in provider.collect(BUILD_ID)["_collector"]
    assert calls == []


def test_repository_and_docker_connection_errors_are_safe_diagnostics():
    class FailedRepository:
        def get_build_resources(self, _build_id):
            raise RuntimeError("mongodb://user:private-password@db")

    result = BuildLogCollector(Settings(), FailedRepository()).collect(BUILD_ID)
    assert result == {"_collector": "Build resource manifest could not be read"}

    def failed_client(_endpoint):
        raise RuntimeError("tcp://private-api-token@docker")

    result = BuildLogCollector(Settings(), Repository(resource()), client_factory=failed_client).collect(BUILD_ID)
    assert result == {"_collector": "Controlled Docker log endpoint is unavailable"}


def test_chunked_logs_keep_only_latest_100_lines_and_close_the_stream():
    lines = [f"line-{index}\n".encode() for index in range(150)]
    service = Service(stream=LogStream(*lines))
    provider, _client, _calls = collector(Repository(resource()), service)
    text = provider.collect(BUILD_ID)["build-web"]
    assert text.splitlines() == [f"line-{index}" for index in range(50, 150)]
    assert service.stream.closed


def test_total_log_byte_limit_is_64k_and_multibyte_text_remains_valid():
    service = Service(stream=LogStream(("数" * 40000 + "\nlatest\n").encode("utf-8")))
    provider, _client, _calls = collector(Repository(resource()), service)
    text = provider.collect(BUILD_ID)["build-web"]
    assert len(text.encode("utf-8")) <= 65536
    assert text.endswith("latest\n")


def test_multiple_services_share_a_total_200_line_limit():
    services = [Service(name=f"web-{index}", native_id=f"id-{index}", stream=LogStream(b"line\n" * 100)) for index in range(3)]
    repo = Repository(*(resource(key=str(index), name=service.name, native_id=service.id) for index, service in enumerate(services)))
    provider, _client, _calls = collector(repo, *services)
    output = provider.collect(BUILD_ID)
    assert sum(len(text.splitlines()) for text in output.values()) <= 200
    assert sum(len(text.encode()) for text in output.values()) <= 65536
    assert len(services[0].log_calls) == 1
    assert len(services[1].log_calls) == 1
    assert services[2].log_calls == []


def test_oversized_or_invalid_docker_stream_fails_safely_and_closes():
    for bad_stream in (LogStream(b"x" * (4 * 1024 * 1024 + 1)), LogStream({"unexpected": "value"})):
        service = Service(stream=bad_stream)
        provider, _client, _calls = collector(Repository(resource()), service)
        assert "could not be read" in provider.collect(BUILD_ID)["build-web"]
        assert bad_stream.closed
