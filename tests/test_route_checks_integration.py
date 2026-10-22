"""Real local Git/CLI processes, MongoDB, Redis, Celery and HTTP integration.

Jenkins, Docker and log collection are explicit controlled adapters; no remote
service, actual model, shared Docker resource or production database is used.
"""

from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from threading import Thread
import time
from types import SimpleNamespace
from uuid import uuid4

from celery.contrib.testing.worker import start_worker
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pymongo import MongoClient
import pytest
import redis

from main import create_app
from orchestrator.celery_app import create_celery_app
from orchestrator.celery_dispatcher import CeleryTaskDispatcher
from orchestrator.config import Settings
from orchestrator.container import create_container
from orchestrator.docker_services import PublishedPort, ServiceDeployment, SwarmDeployment
from orchestrator.gitlab import GitLabHttpAdapter, GitLabProject, GitLabRepositoryRef
from orchestrator.jenkins import JenkinsBuild, JenkinsQueueItem, JenkinsResultArtifact
from orchestrator.models import BuildStatus, ResourceState
from orchestrator.repository import MongoRepository
from orchestrator.route_checks import BUILD_GATE_ERROR, STATIC_ROLE
from orchestrator.worker_runtime import WorkerRuntime


def _free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _until(predicate, *, timeout=20):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"Isolated integration did not reach its expected state; last={last!r}")


@contextmanager
def _daemon(arguments):
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture(scope="module")
def local_databases(tmp_path_factory):
    if not shutil.which("mongod") or not shutil.which("redis-server"):
        pytest.skip("Local mongod and redis-server are needed for the real broker/database integration")
    root = tmp_path_factory.mktemp("route-real-databases")
    mongo_root, redis_root = root / "mongo", root / "redis"
    mongo_root.mkdir()
    redis_root.mkdir()
    mongo_port, redis_port = _free_port(), _free_port()
    mongo_url = f"mongodb://127.0.0.1:{mongo_port}"
    broker_url = f"redis://127.0.0.1:{redis_port}/0"
    mongo = MongoClient(mongo_url, serverSelectionTimeoutMS=150)
    broker = redis.Redis(host="127.0.0.1", port=redis_port, socket_connect_timeout=0.15)
    with _daemon(["mongod", "--bind_ip", "127.0.0.1", "--port", str(mongo_port),
                  "--dbpath", str(mongo_root), "--quiet", "--logpath", str(root / "mongo.log")]), \
         _daemon(["redis-server", "--bind", "127.0.0.1", "--port", str(redis_port),
                  "--save", "", "--appendonly", "no", "--dir", str(redis_root)]):
        def ready():
            try:
                return mongo.admin.command("ping")["ok"] == 1 and broker.ping()
            except Exception:
                return False
        _until(ready, timeout=30)
        try:
            yield mongo_url, broker_url
        finally:
            mongo.close()
            broker.close()


def _git(root, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Route integration",
                           "-c", "user.email=route@example.invalid", "-C", str(root), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class _LocalGit(GitLabHttpAdapter):
    def __init__(self, source):
        super().__init__("https://git.example", "isolated-test-token")
        self.source = source

    def validate_repository_ref(self, repository_url, git_ref):
        commit = _git(self.source, "rev-parse", "--verify", f"{git_ref}^{{commit}}")
        return GitLabRepositoryRef(GitLabProject(1, "test/route-app"), git_ref, commit, repository_url)

    def _clone_url(self, ref):
        return self.source.as_uri()


class _Jenkins:
    def __init__(self):
        self.requests = []
        self.artifact = None

    def trigger_build(self, request):
        self.requests.append(request)
        compose = deepcopy(request.compose)
        images = []
        for name, service in compose["services"].items():
            service.pop("build", None)
            service["image"] = f"{request.image_repository}:{request.build_id}-{name}"
            images.append(service["image"])
        self.artifact = JenkinsResultArtifact(tuple(images), compose, {
            "build_id": request.build_id, "compose": compose, "images": images,
            "image_digests": {image: "sha256:" + "a" * 64 for image in images},
            "source": {"actual_commit": request.git_ref},
        })
        return JenkinsQueueItem("https://jenkins.example/queue/item/1/", 1)

    def get_queue_item(self, queue_url):
        return JenkinsQueueItem(queue_url, 1, build_number=1)

    def get_build(self, number):
        return JenkinsBuild(number, False, "SUCCESS")

    def get_result_artifact(self, number):
        return self.artifact


class _Docker:
    def __init__(self, port):
        self.port = port
        self.calls = []

    def validate_build_compose(self, compose, **kwargs):
        return None

    def deploy_build(self, appid, build_id, compose, tracker):
        self.calls.append(build_id)
        services = []
        for name, service in compose["services"].items():
            physical, identifier = f"route-{build_id}-{name}", f"service-{build_id}-{name}"
            tracker.record("service", physical, native_id=identifier, state=ResourceState.PRESENT,
                           metadata={"compose_service": name})
            services.append(ServiceDeployment(identifier, physical, service["image"], "created",
                f"{physical}:{self.port}", (PublishedPort(8080, self.port),)))
        return SwarmDeployment(appid, f"network-{build_id}", tuple(services))


@pytest.fixture
def integration(tmp_path, local_databases, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.server.calls.append((self.command, self.path))
            passed = self.path == "/health" and not self.server.require_post
            self.send_response(200 if passed else 405)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ready": passed}).encode())

        def do_POST(self):
            self.server.calls.append((self.command, self.path))
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            passed = self.path == "/health" and json.loads(data) == {"ready": True}
            self.send_response(200 if passed else 400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ready": passed}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls, server.require_post = [], False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("def health():\n    return {'ready': True}\n", encoding="utf-8")
    (source / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    _git(source, "init", "--quiet", "--initial-branch=main")
    _git(source, "add", "--all")
    _git(source, "commit", "--quiet", "-m", "Route implementation")
    commit = _git(source, "rev-parse", "HEAD")
    cli = tmp_path / "codex-test-cli"
    cli.write_text(f"#!{sys.executable}\n" + '''import json, pathlib, sys
arguments = sys.argv[1:]
schema = json.loads(pathlib.Path(arguments[arguments.index('--output-schema') + 1]).read_text())
assert schema['required'] == ['response_json']
context = json.JSONDecoder().raw_decode(sys.stdin.read().split('\\n\\nINPUT JSON:\\n', 1)[1])[0]
probe = {'protocol': 'http', 'service_name': 'api', 'target_port': 8080,
         'method': 'GET', 'query_params': {}, 'body': None,
         'assertions': {'expected_status_list': [200], 'json_contains': {'ready': True}}}
if context['task'] == 'static_route_check':
    output = {'route_found': True, 'patch_available': False, 'diff_list': [], 'report_text': '', 'probe_payload': probe}
else:
    assert context['retry_budget'] == 1 and context['container_logs']
    output = {'retry_payload': {'method': 'POST', 'body': {'ready': True}}, 'report_text': 'GET requires a POST JSON request'}
pathlib.Path(arguments[arguments.index('--output-last-message') + 1]).write_text(
    json.dumps({'response_json': json.dumps(output)}))
with pathlib.Path(__file__).with_suffix('.calls').open('a') as calls:
    calls.write(context['task'] + '\\n')
''', encoding="utf-8")
    cli.chmod(0o700)
    mongo_url, broker_url = local_databases
    unique = uuid4().hex
    settings = Settings(environment="test", storage_backend="mongo", task_dispatcher="celery",
        mongodb_url=mongo_url, mongodb_database=f"route_integration_{unique}", celery_broker_url=broker_url,
        celery_build_queue=f"route-tests-{unique}", celery_images_queue=f"route-images-{unique}",
        data_encryption_key=Fernet.generate_key().decode(), codex_executable=str(cli),
        workspace_root=tmp_path / "workspaces", harbor_url="https://harbor.example",
        public_host="127.0.0.1", route_probe_timeout_seconds=2, celery_poll_interval_seconds=0,
        deployment_readiness_timeout_seconds=2)
    app = create_celery_app(settings)
    # Import includes before finalization, matching a normal Celery worker's
    # loader. The embedded testing worker may otherwise finalize an empty app
    # before the separately decorated entry points have been imported.
    app.loader.import_default_modules()
    app.finalize()
    assert {'orchestrator.pipeline.static_route_check', 'orchestrator.pipeline.start_build',
            'orchestrator.pipeline.poll_build', 'orchestrator.pipeline.dynamic_route_check',
            'orchestrator.pipeline.recover_route_checks'} <= set(app.tasks)
    dispatcher = CeleryTaskDispatcher(settings, celery_app=app)
    container = create_container(settings, dispatcher=dispatcher)
    container.applications.gitlab = _LocalGit(source)
    container.dynamic_checks.log_collector = SimpleNamespace(collect=lambda build_id: {build_id: "GET requires POST"})
    jenkins, docker = _Jenkins(), _Docker(server.server_address[1])
    runtime = WorkerRuntime(settings, repository=container.repository, build_service=container.builds,
        catalog=container.catalog, jenkins=jenkins, docker_services=docker,
        route_checks=container.route_checks, dynamic_checks=container.dynamic_checks)
    monkeypatch.setattr("orchestrator.worker_runtime.get_worker_runtime", lambda: runtime)
    monkeypatch.setattr(runtime, "_docker_validation_adapter", lambda: docker)
    try:
        with TestClient(create_app(settings, container)) as client, start_worker(
                app, pool="solo", concurrency=1, perform_ping_check=False,
                queues=[settings.celery_build_queue, settings.celery_images_queue],
                shutdown_timeout=15, loglevel="ERROR"):
            token = client.post("/oauth2/token", json={"client_id": settings.worker_name,
                "client_secret": settings.worker_secret}).json()["access_token"]
            client.headers["Authorization"] = f"Bearer {token}"
            yield SimpleNamespace(client=client, container=container, runtime=runtime, jenkins=jenkins,
                docker=docker, server=server, source=source, commit=commit, cli=cli, settings=settings)
    finally:
        app.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _register(integration):
    response = integration.client.post("/api/v1/jenkins/app_create", json={
        "appid": "route-app", "name": "Real broker integration", "repository_url": "https://git.example/test/app.git",
        "git_ref": "main", "require_route": "/health", "compose": {"services": {"api": {
            "build": {"context": ".", "dockerfile": "Dockerfile"}, "ports": ["8080:8080"],
        }}},
    })
    assert response.status_code == 201, response.text
    repository = integration.container.repository
    def completed():
        check = repository.find_route_check("route-app", integration.commit, "/health", 1)
        return check if check and check.static_status in {"succeeded", "failed"} else None
    check = _until(completed)
    assert check.static_check_passed is True, check.static_report_text
    return check


def test_real_mongo_redis_celery_cli_git_http_delivery_and_single_retry(integration):
    check = _register(integration)
    response = integration.client.post("/api/v1/jenkins/build_create/route-app", json={})
    assert response.status_code == 202, response.text
    build_id = response.json()["build_id"]
    repository = integration.container.repository
    def terminal():
        build = repository.get_build(build_id)
        return build if build.status in {BuildStatus.SUCCEEDED, BuildStatus.FAILED} else None
    build = _until(terminal)
    assert build.status == BuildStatus.SUCCEEDED, build.error
    assert build.dynamic_check_passed is True and build.probe_result["initial"]["response"]["status_code"] == 200
    assert build.git_commit_sha == integration.commit
    assert len(integration.jenkins.requests) == 1 and len(integration.docker.calls) == 1
    assert integration.server.calls == [("GET", "/health")]
    integration.server.require_post = True
    submitted = integration.client.post("/api/v1/apps/route-app/dynamic-checks", json={"build_id": build_id})
    assert submitted.status_code == 202, submitted.text
    rechecked = _until(terminal)
    assert rechecked.status == BuildStatus.SUCCEEDED, rechecked.error
    assert rechecked.dynamic_retry_started is True and rechecked.dynamic_debug_started is True
    assert rechecked.probe_result["initial"]["response"]["status_code"] == 405
    assert rechecked.probe_result["retry"]["response"]["status_code"] == 200
    assert integration.server.calls == [("GET", "/health"), ("GET", "/health"), ("POST", "/health")]
    assert len(integration.docker.calls) == 1, "Manual recheck must reuse its deployment"
    assert integration.cli.with_suffix(".calls").read_text().splitlines() == ["static_route_check", "dynamic_route_debug"]
    details = integration.client.get(f"/api/v1/apps/route-app/commits/{integration.commit}/check-results").json()
    assert details[0]["dynamic_check_passed"] is True
    assert details[0]["probe_result"]["final"]["response"]["status_code"] == 200
    # A new repository client reads the same durable state, not worker memory
    # or a Celery result backend. None of the real external services was used.
    fresh = MongoRepository.connect(integration.settings.mongodb_url, integration.settings.mongodb_database, 1000)
    try:
        assert fresh.get_build(build_id).dynamic_check_passed is True
        assert fresh.get_route_check(check.check_id).dynamic_run_id == rechecked.dynamic_run_id
    finally:
        fresh.close()
    assert _git(integration.source, "status", "--porcelain") == ""


def test_real_broker_recovers_saved_force_generation_and_invalidates_old_gate(integration, monkeypatch):
    check = _register(integration)
    original = integration.container.dispatcher.dispatch_static_check
    def unavailable(*args, **kwargs):
        raise ConnectionError("isolated-producer-offline")
    with monkeypatch.context() as patch:
        patch.setattr(integration.container.dispatcher, "dispatch_static_check", unavailable)
        forced = integration.client.post("/api/v1/apps/route-app/static-checks")
    assert forced.status_code == 503
    pending = integration.container.repository.get_route_check(check.check_id)
    assert pending.generation == 2 and pending.static_check_passed is None and pending.dispatch_error
    blocked = integration.client.post("/api/v1/jenkins/build_create/route-app", json={})
    assert blocked.status_code == 422 and blocked.json()["detail"] == BUILD_GATE_ERROR
    # The actual Redis/Celery worker consumes recovery and dispatches the saved
    # generation; no in-memory dispatcher or eager execution is involved.
    app = integration.container.dispatcher.celery_app
    app.send_task("orchestrator.pipeline.recover_route_checks", args=[], queue=integration.settings.celery_build_queue)
    def recovered():
        current = integration.container.repository.get_route_check(check.check_id)
        return current if current.static_status == "succeeded" and current.generation == 2 else None
    assert _until(recovered).static_check_passed is True
    assert integration.container.dispatcher.dispatch_static_check == original
    role = integration.client.put(f"/api/v1/agent-roles/{STATIC_ROLE}", json={"prompt": "New static role version"})
    assert role.status_code == 200 and role.json()["agent_role_version"] == 2
    # DELETE only looks at the latest role/route dimensions, not historical v1.
    deleted = integration.client.delete(f"/api/v1/apps/route-app/commits/{integration.commit}/check-results")
    assert deleted.status_code == 200 and deleted.json() is False
    assert integration.container.repository.get_route_check(check.check_id) is not None
