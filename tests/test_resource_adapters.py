from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import ssl
from threading import Thread
from types import SimpleNamespace

import pytest

from orchestrator.build_resources import ResourceConflict, ResourceTracker, build_lease
from orchestrator.config import Settings
from orchestrator.gitlab import HttpResponse, TransportError
from orchestrator.harbor_cleanup import HarborCleanupError, HarborImageCleaner
from orchestrator.models import BuildResources, CleanupStatus, ResourceState, utc_now
from orchestrator.repository import MongoRepository, RepositoryError


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def harbor_cleaner(*responses, guard=None):
    settings = Settings(harbor_url="https://registry.example:8443", harbor_project="apps", harbor_user="robot", harbor_password="secret")
    transport = FakeTransport(*responses)
    return HarborImageCleaner(settings, transport=transport, guard=guard), transport


BUILD_ID = "a" * 32
REPOSITORY = "registry.example:8443/apps/nested/demo"
REFERENCE = REPOSITORY + ":" + BUILD_ID + "-web"
DIGEST = "sha256:" + "b" * 64


def artifact_response(*tags, digest=DIGEST):
    return HttpResponse(200, body=json.dumps({"digest": digest, "tags": [{"name": tag} for tag in tags]}).encode())


@pytest.mark.parametrize("shared", [False, True])
def test_harbor_deletes_only_build_tag_using_verified_digest_and_encoded_repository(shared):
    tag = REFERENCE.rsplit(":", 1)[1]
    cleaner, transport = harbor_cleaner(artifact_response(tag, *(["another-build-web"] if shared else [])), HttpResponse(204), HttpResponse(404))
    assert cleaner.remove_build_tag(REFERENCE, REPOSITORY, BUILD_ID, DIGEST) == ("shared_retained" if shared else "waiting_gc")
    assert [call[0] for call in transport.calls] == ["GET", "DELETE", "GET"]
    assert "/repositories/nested%252Fdemo/" in transport.calls[0][1]
    assert "/artifacts/sha256%3A" in transport.calls[1][1]
    assert transport.calls[1][1].endswith("/tags/" + tag)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_harbor_inspection_failure_does_not_delete_or_leak_credentials(status):
    cleaner, transport = harbor_cleaner(HttpResponse(status))
    with pytest.raises(HarborCleanupError) as error:
        cleaner.remove_build_tag(REFERENCE, REPOSITORY, BUILD_ID, DIGEST)
    assert "secret" not in str(error.value)
    assert len(transport.calls) == 1


def test_harbor_does_not_report_success_if_tag_reappears_after_deletion():
    cleaner, _ = harbor_cleaner(artifact_response(), HttpResponse(204), artifact_response(digest="sha256:" + "c" * 64))
    with pytest.raises(HarborCleanupError, match="could not be verified"):
        cleaner.remove_build_tag(REFERENCE, REPOSITORY, BUILD_ID, DIGEST)


def test_harbor_missing_tag_is_idempotent():
    cleaner, transport = harbor_cleaner(HttpResponse(404))
    assert cleaner.remove_build_tag(REFERENCE, REPOSITORY, BUILD_ID) == "waiting_gc"
    assert len(transport.calls) == 1


def test_harbor_digest_replacement_and_lease_loss_prevent_deletion():
    cleaner, transport = harbor_cleaner(artifact_response(digest="sha256:" + "c" * 64))
    with pytest.raises(HarborCleanupError, match="digest changed"):
        cleaner.remove_build_tag(REFERENCE, REPOSITORY, BUILD_ID, DIGEST)
    assert len(transport.calls) == 1

    def lost_lease():
        raise ResourceConflict("Build resource lease lost")

    cleaner, transport = harbor_cleaner(artifact_response(), guard=lost_lease)
    with pytest.raises(ResourceConflict):
        cleaner.remove_build_tag(REFERENCE, REPOSITORY, BUILD_ID, DIGEST)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("reference", ["other.example/apps/demo:" + BUILD_ID + "-web", REPOSITORY + ":latest", REPOSITORY + ":" + "d" * 32 + "-web"])
def test_harbor_rejects_unowned_reference_without_network_io(reference):
    cleaner, transport = harbor_cleaner()
    with pytest.raises(HarborCleanupError):
        cleaner.remove_build_tag(reference, REPOSITORY, BUILD_ID)
    assert not transport.calls


def test_harbor_transport_errors_are_redacted():
    cleaner, _ = harbor_cleaner(RuntimeError("https://robot:secret@registry.example"))
    with pytest.raises(HarborCleanupError, match="request failed") as error:
        cleaner.remove_build_tag(REFERENCE, REPOSITORY, BUILD_ID)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("matching_host", [True, False])
def test_harbor_explicit_trust_anchor_preserves_tls_verification(tmp_path, matching_host):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    issuer_key = ec.generate_private_key(ec.SECP256R1())
    server_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Harbor test")])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Private issuer")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(utc_now() - timedelta(minutes=1))
        .not_valid_after(utc_now() + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1" if matching_host else "127.0.0.2"))]), critical=False)
        .sign(issuer_key, hashes.SHA256())
    )
    certificate_path = tmp_path / "harbor.crt"
    key_path = tmp_path / "harbor.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"trusted")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(harbor_url=f"https://127.0.0.1:{server.server_port}", harbor_ca_file=certificate_path)
    try:
        cleaner = HarborImageCleaner(settings)
        if matching_host:
            assert cleaner.transport.request("GET", settings.harbor_url, headers={}).status_code == 200
        else:
            with pytest.raises(TransportError):
                cleaner.transport.request("GET", settings.harbor_url, headers={})
        untrusted = HarborImageCleaner(settings.model_copy(update={"harbor_ca_file": None}))
        with pytest.raises(TransportError):
            untrusted.transport.request("GET", settings.harbor_url, headers={})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_harbor_invalid_trust_configuration_fails_closed_and_empty_setting_uses_defaults(tmp_path):
    assert Settings(harbor_ca_file="").harbor_ca_file is None
    settings = Settings(harbor_url="https://registry.example", harbor_ca_file=tmp_path / "missing-secret.crt")
    with pytest.raises(HarborCleanupError, match="certificate trust configuration") as error:
        HarborImageCleaner(settings)
    assert "missing-secret" not in str(error.value)


def test_harbor_basic_auth_cleanup_does_not_reuse_browser_session_cookies():
    cookies = []

    class Handler(BaseHTTPRequestHandler):
        deleted = False

        def do_GET(self):
            cookies.append(self.headers.get("Cookie"))
            self.send_response(404 if Handler.deleted else 200)
            self.send_header("Set-Cookie", "sid=browser-session; Path=/")
            self.end_headers()
            if not Handler.deleted:
                self.wfile.write(json.dumps({"digest": DIGEST, "tags": [{"name": BUILD_ID + "-web"}]}).encode())

        def do_DELETE(self):
            cookies.append(self.headers.get("Cookie"))
            Handler.deleted = not self.headers.get("Cookie") and self.headers.get("Authorization", "").startswith("Basic ")
            self.send_response(204 if Handler.deleted else 403)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(harbor_url=f"http://127.0.0.1:{server.server_port}", harbor_project="apps", harbor_user="robot", harbor_password="secret")
    repository = f"127.0.0.1:{server.server_port}/apps/demo"
    try:
        cleaner = HarborImageCleaner(settings)
        assert cleaner.remove_build_tag(repository + ":" + BUILD_ID + "-web", repository, BUILD_ID, DIGEST) == "waiting_gc"
        assert cookies == [None, None, None]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def mongo_resources():
    mongomock = pytest.importorskip("mongomock")
    client = mongomock.MongoClient()
    repository = MongoRepository(client.test)
    repository.ensure_indexes()
    repository.ensure_build_resources(BuildResources(build_id=BUILD_ID, appid="demo"))
    yield repository
    client.close()


def test_mongo_manifest_lease_and_resource_updates_are_fenced(mongo_resources):
    repository = mongo_resources
    assert repository.ensure_build_resources(BuildResources(build_id=BUILD_ID, appid="demo", repository_url="changed")).repository_url is None
    with pytest.raises(RepositoryError):
        repository.ensure_build_resources(BuildResources(build_id=BUILD_ID, appid="another"))
    with build_lease(repository, BUILD_ID, [CleanupStatus.NOT_STARTED]) as tracker:
        assert tracker is not None
        assert repository.claim_build_lease(BUILD_ID, "competitor", utc_now() + timedelta(seconds=60), [CleanupStatus.NOT_STARTED]) is None
        resource = tracker.record("network", "private-network", native_id="network-id", state=ResourceState.PRESENT)
        tracker.guard()
        assert repository.renew_build_lease(BUILD_ID, tracker.owner, utc_now() + timedelta(seconds=120))
        repository.update_build_resources(BUILD_ID, {"lease_expires_at": utc_now() - timedelta(seconds=1)})
        with pytest.raises(RepositoryError, match="lease lost"):
            tracker.update(resource, ResourceState.REMOVED)
        replacement = repository.claim_build_lease(BUILD_ID, "new-owner", utc_now() + timedelta(seconds=60), [CleanupStatus.NOT_STARTED])
        assert replacement is not None
        repository.release_build_lease(BUILD_ID, tracker.owner)
        assert repository.get_build_resources(BUILD_ID).lease_owner == "new-owner"
        assert repository.update_build_resources(BUILD_ID, {"cleanup_status": CleanupStatus.SUCCEEDED}, {"lease_owner": tracker.owner}) is None


def test_mongo_port_reservations_and_recovery_queries(mongo_resources):
    repository = mongo_resources
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda owner: repository.reserve_port("docker:engine", "tcp", 18080, str(owner), "web"), range(32)))
    assert sum(results) == 1
    winner = str(results.index(True))
    assert repository.reserve_port("docker:engine", "tcp", 18080, winner, "web")
    assert not repository.reserve_port("docker:engine", "tcp", 18080, winner, "another-service")
    assert len(repository.list_build_ports(winner)) == 1
    assert not repository.release_port("docker:engine", "tcp", 18080, "wrong-owner")
    assert repository.release_port("docker:engine", "tcp", 18080, winner)
    assert not repository.list_build_ports(winner)
    repository.update_build_resources(BUILD_ID, {"cleanup_status": CleanupStatus.QUEUED})
    assert [manifest.build_id for manifest in repository.list_cleanup_resources()] == [BUILD_ID]
    repository.update_build_resources(BUILD_ID, {"cleanup_status": CleanupStatus.SUCCEEDED})
    assert repository.list_cleanup_resources() == []


@pytest.mark.parametrize("key", ["bad.key", "$update", "a" * 63, "A" * 64])
def test_mongo_rejects_invalid_resource_keys_before_writing(mongo_resources, key):
    with pytest.raises(ValueError):
        mongo_resources.put_build_resource(BUILD_ID, SimpleNamespace(key=key))


def test_mongo_concurrent_cleanup_has_one_dispatch_and_durable_lease(mongo_resources, tmp_path):
    from orchestrator.build_resources import BuildResourceService
    from orchestrator.models import BuildJob, BuildStatus
    from orchestrator.tasks import InMemoryTaskDispatcher
    from orchestrator.workspaces import WorkspaceStore

    repository = mongo_resources
    repository.create_build(BuildJob(build_id=BUILD_ID, appid="demo", git_ref="main", status=BuildStatus.FAILED))
    dispatcher = InMemoryTaskDispatcher()
    service = BuildResourceService(repository, dispatcher, WorkspaceStore.from_settings(Settings(workspace_root=tmp_path)))

    def request():
        try:
            return service.request_cleanup(BUILD_ID)[1]
        except ResourceConflict:
            return 409

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(lambda number: request(), range(16)))
    assert set(statuses) <= {202, 409}
    assert dispatcher.cleanup_build_ids == [BUILD_ID]
    manifest = repository.get_build_resources(BUILD_ID)
    assert manifest.cleanup_status == CleanupStatus.QUEUED and manifest.cleanup_generation == 1
    assert manifest.lease_owner is None
