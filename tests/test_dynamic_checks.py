"""Dynamic quality-gate unit and in-process integration tests.

External requests use genuine local HTTP/gRPC servers in the final tests.
Codex execution and Docker cleanup are explicit test doubles; these tests do
not claim a real Codex invocation or access a developer's Docker daemon.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
import pytest

from orchestrator.agent_executor import AgentExecutionError
from orchestrator.build_resources import BuildCleanupRunner, ResourceConflict, ResourceTracker, build_lease
from orchestrator.config import Settings
from orchestrator.container import create_container
from orchestrator.dynamic_checks import DynamicCheckService
from orchestrator.models import (
    AgentRoleUpdate, BuildJob, BuildResources, BuildStatus, CleanupStatus,
    DeploymentService, PublishedPort, ResourceState, RouteCheckResult, UserAppRecord, utc_now,
)
from orchestrator.route_checks import DYNAMIC_ROLE, STATIC_ROLE, RouteCheckConflict, RouteCheckInputError, RouteCheckUnavailableError
from orchestrator.route_probes import ProbeClient, ProbeEndpoint, validate_probe_payload
from orchestrator.services import NotFoundError
from orchestrator.tasks import InMemoryTaskDispatcher
from orchestrator.worker_runtime import WorkerRuntime


APP_ID = "dynamic-gate"
COMMIT = "a" * 40
BUILD_ID = "1" * 32


def payload(**changes):
    return {
        "method": "POST", "query_params": {}, "body": {"phase": "initial"},
        "assertions": {"expected_status_list": [200]}, **changes,
    }


def passed(**changes):
    return {"passed": True, "response": {"status_code": 200, "body": "ready"}, **changes}


def failed(**changes):
    return {"passed": False, "response": {"status_code": 503, "body": "starting"}, "error_type": "assertion_failed", **changes}


class WorkerCrash(BaseException):
    """Abrupt worker loss must bypass normal Exception recovery handlers."""


class SequenceProbe:
    def __init__(self, *results, before_call=None):
        self.results = list(results)
        self.calls = []
        self.before_call = before_call

    def run(self, request, *, require_route, endpoints):
        self.calls.append({"payload": deepcopy(request), "require_route": require_route, "endpoints": list(endpoints)})
        if self.before_call:
            self.before_call(len(self.calls))
        if not self.results:
            pytest.fail("An unexpected additional probe call consumed the one-retry budget")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result() if callable(result) else deepcopy(result)


class RecordingAgent:
    def __init__(self, output=None, *, before_call=None):
        self.output = output if output is not None else {"retry_payload": None, "report_text": "No safe recovery"}
        self.calls = []
        self.before_call = before_call

    def run_json(self, role, context, *, cwd):
        directory = Path(cwd)
        assert directory.is_dir()
        self.calls.append({"role": role.model_copy(deep=True), "context": deepcopy(context), "cwd": directory})
        if self.before_call:
            self.before_call()
        if isinstance(self.output, BaseException):
            raise self.output
        return deepcopy(self.output)


class RecordingLogs:
    def __init__(self, output=None):
        self.output = output or {"web": "latest build-owned stdout\nconnection refused"}
        self.calls = []

    def collect(self, build_id):
        self.calls.append(build_id)
        return deepcopy(self.output)


class ToggleDispatcher(InMemoryTaskDispatcher):
    def __init__(self):
        super().__init__()
        self.offline = False
        self.dynamic_attempts = []
        self.cleanup_attempts = []
        self.before_dynamic = None

    def dispatch_dynamic_check(self, build_id, run_id=None):
        self.dynamic_attempts.append((build_id, run_id))
        if self.before_dynamic:
            self.before_dynamic()
        if self.offline:
            raise RuntimeError("redis://user:broker-private-password@broker is unavailable")
        return super().dispatch_dynamic_check(build_id, run_id)

    def dispatch_build_cleanup(self, build_id):
        self.cleanup_attempts.append(build_id)
        if self.offline:
            raise RuntimeError("broker-private-password")
        return super().dispatch_build_cleanup(build_id)


def add_build(system, *, build_id=BUILD_ID, service_id=None, status=BuildStatus.DEPLOYING, **changes):
    check = system.repository.get_route_check(system.check_id)
    role = system.container.route_checks.roles.get(DYNAMIC_ROLE)
    service_id = service_id or "service-" + build_id[:8]
    service_name = "swarm-" + build_id[:8] + "-web"
    build = BuildJob(**{
        "build_id": build_id, "appid": APP_ID, "git_ref": "main", "git_commit_sha": COMMIT,
        "status": status, "compose_snapshot": {"services": {"web": {"image": "test/web", "ports": ["8000"]}}},
        "service_ids": [service_id], "route_check_id": check.check_id, "route_check_generation": check.generation,
        "require_route_snapshot": check.require_route, "static_agent_role_version": check.static_agent_role_version,
        "dynamic_agent_role_version": role.agent_role_version, "route_dynamic_role_snapshot": role.model_dump(mode="json"),
        "route_probe_payload": deepcopy(check.probe_payload), "static_check_passed": True, **changes,
    })
    system.repository.create_build(build)
    system.repository.ensure_build_resources(BuildResources(
        build_id=build_id, appid=APP_ID, environment_ciphertext=system.encrypted_environment,
    ))
    tracker = ResourceTracker(system.repository, build_id)
    tracker.record("service", service_name, native_id=service_id, target="docker:controlled-engine",
                   state=ResourceState.PRESENT, metadata={"compose_service": "web"})
    system.repository.save_service(DeploymentService(
        service_id=service_id, service_name=service_name, appid=APP_ID, build_id=build_id,
        image="test/web:build", status="deployed", endpoint="http://untrusted-endpoint.example/ignored",
        published_ports=[PublishedPort(target_port=8000, published_port=18080)],
    ))
    return build


@pytest.fixture
def system_factory(tmp_path):
    def make(*, request=None, route="/core", **setting_changes):
        settings = Settings(**{
            "workspace_root": tmp_path / uuid4().hex, "public_host": "public.example",
            "route_probe_timeout_seconds": 2, **setting_changes,
        })
        dispatcher = ToggleDispatcher()
        container = create_container(settings, dispatcher=dispatcher)
        encrypted = container.secret_box.encrypt_environment({"APP_TOKEN": "runtime-private-secret"})
        app = UserAppRecord(
            appid=APP_ID, name="Dynamic quality gate", repository_url="https://git.example/team/app.git",
            git_ref="main", require_route=route, environment_ciphertext=encrypted,
            compose={"services": {"web": {"image": "test/web", "ports": ["8000"]}}},
        )
        container.repository.create_app(app)
        static_role = container.route_checks.roles.get(STATIC_ROLE)
        dynamic_role = container.route_checks.roles.get(DYNAMIC_ROLE)
        check = RouteCheckResult(
            check_id=uuid4().hex, app_id=APP_ID, commit_id=COMMIT, require_route=route,
            repository_url=app.repository_url, static_agent_role_version=static_role.agent_role_version,
            dynamic_agent_role_version=dynamic_role.agent_role_version,
            static_role_snapshot=static_role.model_dump(mode="json"), dynamic_role_snapshot=dynamic_role.model_dump(mode="json"),
            static_check_passed=True, static_status="succeeded", probe_payload=deepcopy(request or payload()),
        )
        container.repository.save_route_check(check)
        system = SimpleNamespace(
            container=container, repository=container.repository, settings=settings,
            dispatcher=dispatcher, check_id=check.check_id, app=app, encrypted_environment=encrypted,
        )
        add_build(system)
        return system

    return make


def service(system, probe=None, agent=None, logs=None):
    return DynamicCheckService(
        system.repository, system.dispatcher, system.settings, system.container.route_checks,
        system.container.builds, system.container.build_resources,
        probe_client=probe or SequenceProbe(), executor=agent or RecordingAgent(), log_collector=logs or RecordingLogs(),
    )


def execute(system, dynamic, build_id=BUILD_ID):
    with build_lease(system.repository, build_id, [CleanupStatus.NOT_STARTED]) as tracker:
        assert tracker is not None
        return dynamic.execute(build_id, guard=tracker.guard)


def runtime(system, dynamic):
    return WorkerRuntime(
        system.settings, repository=system.repository, build_service=system.container.builds,
        catalog=system.container.catalog, route_checks=system.container.route_checks, dynamic_checks=dynamic,
    )


def test_initial_success_saves_raw_evidence_without_logs_or_debug_agent(system_factory):
    system = system_factory()
    raw = passed(response={"status_code": 200, "headers": {"x-ready": "yes"}, "body": "raw success"})
    probe, agent, logs = SequenceProbe(raw), RecordingAgent(), RecordingLogs()

    def assert_reserved(_count):
        saved = system.repository.get_build(BUILD_ID)
        assert saved.dynamic_initial_started is True
        assert saved.dynamic_status == "running"
        assert saved.dynamic_run_id

    probe.before_call = assert_reserved
    result = execute(system, service(system, probe, agent, logs))
    assert result.dynamic_check_passed is True
    assert result.dynamic_status == "succeeded"
    assert result.probe_result == {"initial": raw, "retry": None, "final": raw}
    assert result.teardown_required is False
    assert result.dynamic_report_text is None
    assert len(probe.calls) == 1 and not agent.calls and not logs.calls
    check = system.repository.get_route_check(system.check_id)
    assert check.dynamic_build_id == BUILD_ID
    assert check.dynamic_run_id == result.dynamic_run_id
    assert check.dynamic_check_passed is True
    assert check.probe_result == result.probe_result


def test_debug_receives_persisted_initial_failure_and_build_logs_then_one_retry_succeeds(system_factory):
    system = system_factory()
    initial, retry = failed(), passed()
    probe = SequenceProbe(initial, retry)
    logs = RecordingLogs()
    agent = RecordingAgent({"retry_payload": {"body": {"phase": "ready"}}, "report_text": "Payload requires phase=ready"})

    def inspect_agent_reservation():
        saved = system.repository.get_build(BUILD_ID)
        assert saved.probe_result["initial"] == initial
        assert saved.probe_result["container_logs"] == logs.output
        assert saved.dynamic_debug_started is True
        assert saved.dynamic_retry_started is False
        assert system.repository.get_route_check(system.check_id).probe_result["initial"] == initial

    agent.before_call = inspect_agent_reservation
    dynamic = service(system, probe, agent, logs)
    result = execute(system, dynamic)
    assert result.dynamic_check_passed is True and result.dynamic_status == "succeeded"
    assert result.probe_result["initial"] == initial
    assert result.probe_result["retry"] == retry == result.probe_result["final"]
    assert result.probe_result["container_logs"] == logs.output
    assert result.dynamic_report_text is None
    assert result.dynamic_retry_started is True
    assert len(probe.calls) == 2 and len(agent.calls) == 1 and logs.calls == [BUILD_ID]
    assert probe.calls[1]["payload"]["body"] == {"phase": "ready"}
    assert probe.calls[0]["payload"]["assertions"] == probe.calls[1]["payload"]["assertions"]
    context = agent.calls[0]["context"]
    assert context["retry_budget"] == 1
    assert context["build_id"] == BUILD_ID
    assert context["require_route"] == "/core"
    assert context["probe_result"]["initial"] == initial
    assert context["container_logs"] == logs.output
    assert not agent.calls[0]["cwd"].exists()


def test_retry_failure_marks_delivery_failed_with_diagnosis_and_teardown_requirement(system_factory):
    system = system_factory()
    probe = SequenceProbe(failed(), failed(response={"status_code": 500, "body": "still broken"}))
    agent = RecordingAgent({"retry_payload": {"body": {"phase": "ready"}}, "report_text": "Database connection unavailable"})
    dynamic = service(system, probe, agent)
    result = execute(system, dynamic)
    assert result.status == BuildStatus.FAILED
    assert result.dynamic_status == "failed" and result.dynamic_check_passed is False
    assert result.teardown_required is True and result.teardown_status == "pending"
    assert "Database connection unavailable" in result.dynamic_report_text
    assert "唯一一次" in result.dynamic_report_text
    assert result.error == result.dynamic_report_text
    assert result.probe_result["final"] == result.probe_result["retry"]
    check = system.repository.get_route_check(system.check_id)
    assert check.dynamic_check_passed is False and check.dynamic_report_text == result.dynamic_report_text
    assert len(system.repository.list_alerts(APP_ID)) == 1
    # A duplicate delivery must not create another alert or external request.
    assert execute(system, dynamic).probe_result == result.probe_result
    assert len(probe.calls) == 2 and len(agent.calls) == 1
    assert len(system.repository.list_alerts(APP_ID)) == 1


def test_no_safe_retry_still_keeps_diagnosis_and_does_not_issue_second_probe(system_factory):
    system = system_factory()
    probe = SequenceProbe(failed())
    agent = RecordingAgent({"retry_payload": None, "report_text": "Target route is absent from the running image"})
    result = execute(system, service(system, probe, agent))
    assert result.dynamic_check_passed is False and result.status == BuildStatus.FAILED
    assert result.dynamic_report_text == "Target route is absent from the running image"
    assert result.probe_result["retry"] is None
    assert len(probe.calls) == 1 and len(agent.calls) == 1


def test_rejected_retry_keeps_the_agent_diagnosis_in_build_and_check_results(system_factory):
    system = system_factory()
    diagnosis = "The running image has no registered handler for the required route"
    agent = RecordingAgent({
        "retry_payload": {"assertions": {"expected_status_list": [200, 500]}},
        "report_text": diagnosis,
    })
    probe = SequenceProbe(failed())
    result = execute(system, service(system, probe, agent))
    assert result.status == BuildStatus.FAILED and result.teardown_required
    assert diagnosis in result.dynamic_report_text
    assert "cannot change" in result.dynamic_report_text
    assert system.repository.get_route_check(system.check_id).dynamic_report_text == result.dynamic_report_text
    assert not result.dynamic_retry_started and len(probe.calls) == len(agent.calls) == 1


def test_interrupted_retry_validation_keeps_the_saved_diagnosis_without_repeating_agent(system_factory, monkeypatch):
    system = system_factory()
    diagnosis = "The application requires an explicit phase in the request body"
    agent = RecordingAgent({"retry_payload": {"body": {"phase": "ready"}}, "report_text": diagnosis})
    probe = SequenceProbe(failed())
    dynamic = service(system, probe, agent)
    original_validation = dynamic._retry_payload

    def crash_after_agent(*_args):
        raise WorkerCrash()

    monkeypatch.setattr(dynamic, "_retry_payload", crash_after_agent)
    with pytest.raises(WorkerCrash):
        execute(system, dynamic)
    interrupted = system.repository.get_build(BUILD_ID)
    assert interrupted.dynamic_report_text == diagnosis
    assert interrupted.dynamic_debug_started and interrupted.dynamic_retry_payload is None
    monkeypatch.setattr(dynamic, "_retry_payload", original_validation)
    result = execute(system, dynamic)
    assert result.status == BuildStatus.FAILED and diagnosis in result.dynamic_report_text
    assert "中断" in result.dynamic_report_text
    assert len(probe.calls) == len(agent.calls) == 1


@pytest.mark.parametrize("stage,expected_calls", [("initial", 1), ("debug", 1), ("retry", 2)])
def test_crashed_external_calls_are_reserved_and_never_repeated(system_factory, stage, expected_calls):
    system = system_factory()
    probe_results = [WorkerCrash()] if stage == "initial" else [failed(), WorkerCrash()]
    probe = SequenceProbe(*probe_results)
    agent = RecordingAgent(WorkerCrash() if stage == "debug" else {"retry_payload": {"body": {"phase": "ready"}}, "report_text": "retry safely"})
    dynamic = service(system, probe, agent)
    with pytest.raises(WorkerCrash):
        execute(system, dynamic)
    interrupted = system.repository.get_build(BUILD_ID)
    assert interrupted.dynamic_status == "running"
    assert interrupted.dynamic_initial_started is True
    assert interrupted.dynamic_debug_started is (stage != "initial")
    assert interrupted.dynamic_retry_started is (stage == "retry")
    assert system.repository.get_build_resources(BUILD_ID).lease_owner is None
    result = execute(system, dynamic)
    assert result.dynamic_status == "failed" and result.teardown_required is True
    assert "中断" in result.dynamic_report_text
    assert len(probe.calls) == expected_calls
    assert len(agent.calls) == (0 if stage == "initial" else 1)
    execute(system, dynamic)
    assert len(probe.calls) == expected_calls
    assert len(agent.calls) == (0 if stage == "initial" else 1)


def test_saved_debug_payload_recovers_without_reinvoking_the_agent(system_factory):
    system = system_factory()
    system.repository.update_build(BUILD_ID, {
        "dynamic_initial_started": True, "dynamic_debug_started": True, "dynamic_status": "running",
        "dynamic_retry_payload": {**payload(), "body": {"phase": "ready"}},
        "probe_result": {"initial": failed(), "retry": None, "final": failed()},
    })
    probe, agent = SequenceProbe(passed()), RecordingAgent()
    result = execute(system, service(system, probe, agent))
    assert result.dynamic_check_passed is True
    assert len(probe.calls) == 1 and agent.calls == []
    assert probe.calls[0]["payload"]["body"] == {"phase": "ready"}


@pytest.mark.parametrize("successful", [True, False])
def test_saved_retry_response_recovers_without_repeating_any_external_call(system_factory, successful):
    system = system_factory()
    response = passed() if successful else failed()
    system.repository.update_build(BUILD_ID, {
        "dynamic_initial_started": True, "dynamic_debug_started": True, "dynamic_retry_started": True,
        "dynamic_status": "running", "dynamic_report_text": "Saved failure analysis",
        "probe_result": {"initial": failed(), "retry": response, "final": response},
    })
    probe, agent, logs = SequenceProbe(), RecordingAgent(), RecordingLogs()
    result = execute(system, service(system, probe, agent, logs))
    assert result.dynamic_check_passed is successful
    assert result.teardown_required is (not successful)
    assert probe.calls == agent.calls == logs.calls == []


def test_lost_build_lease_does_not_persist_a_response_or_repeat_uncertain_io(system_factory):
    system = system_factory()

    def lose_lease(_count):
        manifest = system.repository.get_build_resources(BUILD_ID)
        system.repository.release_build_lease(BUILD_ID, manifest.lease_owner)

    probe = SequenceProbe(passed(), before_call=lose_lease)
    dynamic = service(system, probe)
    with pytest.raises(ResourceConflict):
        execute(system, dynamic)
    interrupted = system.repository.get_build(BUILD_ID)
    assert interrupted.dynamic_check_passed is None
    assert interrupted.dynamic_initial_started is True
    assert interrupted.probe_result is None
    assert interrupted.teardown_required is False
    result = execute(system, dynamic)
    assert result.dynamic_check_passed is False and result.teardown_required is True
    assert len(probe.calls) == 1


def test_terminal_success_reconciles_an_interrupted_aggregate_write_without_new_io(system_factory, monkeypatch):
    system = system_factory()
    probe, agent, logs = SequenceProbe(passed()), RecordingAgent(), RecordingLogs()
    dynamic = service(system, probe, agent, logs)
    original_sync = dynamic._sync_result

    def crash_before_final_aggregate(build, guard):
        if build.dynamic_status == "succeeded":
            raise WorkerCrash()
        return original_sync(build, guard)

    monkeypatch.setattr(dynamic, "_sync_result", crash_before_final_aggregate)
    with pytest.raises(WorkerCrash):
        execute(system, dynamic)
    assert system.repository.get_build(BUILD_ID).dynamic_check_passed is True
    assert system.repository.get_route_check(system.check_id).dynamic_check_passed is None
    monkeypatch.setattr(dynamic, "_sync_result", original_sync)
    result = runtime(system, dynamic).dynamic_route_check(BUILD_ID)
    assert result["status"] == BuildStatus.SUCCEEDED.value
    check = system.repository.get_route_check(system.check_id)
    assert check.dynamic_status == "succeeded" and check.dynamic_check_passed is True
    assert check.probe_result == system.repository.get_build(BUILD_ID).probe_result
    assert len(probe.calls) == 1 and not agent.calls and not logs.calls


@pytest.mark.parametrize("retry", [
    {"assertions": {"expected_status_list": [200, 500]}},
    {"assertions": {"expected_status_list": [200]}},
    {"path": "/other"}, {"url": "http://unowned.example/core"},
    {"protocol": "grpc"}, {"service_name": "db"}, {"target_port": 9999},
])
def test_debug_cannot_weaken_assertions_or_change_route_protocol_or_selected_endpoint(system_factory, retry):
    system = system_factory(request=payload(service_name="web", target_port=8000, assertions={"expected_status_list": [200], "json_contains": {"ok": True}}))
    probe = SequenceProbe(failed())
    agent = RecordingAgent({"retry_payload": retry, "report_text": "Unsafe proposed recovery"})
    result = execute(system, service(system, probe, agent))
    assert result.dynamic_check_passed is False and result.teardown_required is True
    assert len(probe.calls) == 1
    assert result.dynamic_retry_started is False


@pytest.mark.parametrize("assertion", ["json_equals", "json_contains", "json_path_equals"])
def test_retry_assertion_comparison_is_type_sensitive_not_python_boolean_number_equality(system_factory, assertion):
    original = {"expected_status_list": [200], assertion: {"ok": True}}
    system = system_factory(request=payload(assertions=original))
    agent = RecordingAgent({"retry_payload": {"assertions": {"expected_status_list": [200], assertion: {"ok": 1}}}, "report_text": "Do not substitute a number for the original boolean"})
    probe = SequenceProbe(failed(), passed())
    result = execute(system, service(system, probe, agent))
    assert result.dynamic_check_passed is False
    assert result.dynamic_retry_started is False
    assert len(probe.calls) == 1


@pytest.mark.parametrize("original_reflection,retry_reflection", [(None, True), (None, False), (True, False), (False, True)])
def test_debug_cannot_add_or_change_the_frozen_grpc_reflection_policy(system_factory, original_reflection, retry_reflection):
    original = {"protocol": "grpc", "body_base64": "", "assertions": {"expected_grpc_status_list": ["OK"]}}
    if original_reflection is not None:
        original["reflection"] = original_reflection
    system = system_factory(route="/fixed.Route/Check", request=original)
    agent = RecordingAgent({"retry_payload": {"reflection": retry_reflection}, "report_text": "Reflection policy cannot be changed by the retry"})
    probe = SequenceProbe(failed(), passed())
    result = execute(system, service(system, probe, agent))
    assert result.dynamic_check_passed is False
    assert result.dynamic_retry_started is False
    assert result.teardown_required is True
    assert len(probe.calls) == 1 and len(agent.calls) == 1


@pytest.mark.parametrize("reflection", [True, False])
def test_debug_may_repair_payload_while_preserving_an_explicit_grpc_reflection_policy(system_factory, reflection):
    original = {"protocol": "grpc", "body_base64": "", "reflection": reflection, "assertions": {"expected_grpc_status_list": ["OK"]}}
    system = system_factory(route="/fixed.Route/Check", request=original)
    agent = RecordingAgent({"retry_payload": {"body_base64": "CgF4", "reflection": reflection}, "report_text": "Preserve the frozen output interpretation"})
    probe = SequenceProbe(failed(), passed())
    result = execute(system, service(system, probe, agent))
    assert result.dynamic_check_passed is True
    assert len(probe.calls) == 2
    assert probe.calls[1]["payload"]["reflection"] is reflection


def test_debug_uses_frozen_role_version_even_after_current_role_is_updated(system_factory):
    system = system_factory()
    frozen = system.repository.get_build(BUILD_ID).route_dynamic_role_snapshot
    updated = system.container.route_checks.roles.update(DYNAMIC_ROLE, AgentRoleUpdate(prompt="A newer prompt must not affect an existing build"))
    assert updated.agent_role_version == frozen["agent_role_version"] + 1
    agent = RecordingAgent({"retry_payload": None, "report_text": "Frozen role diagnosis"})
    execute(system, service(system, SequenceProbe(failed()), agent))
    assert agent.calls[0]["role"].agent_role_version == frozen["agent_role_version"]
    assert agent.calls[0]["role"].prompt == frozen["prompt"]
    assert system.repository.get_build(BUILD_ID).dynamic_agent_role_version == frozen["agent_role_version"]


def test_mismatched_frozen_role_is_rejected_before_agent_call(system_factory):
    system = system_factory()
    system.repository.update_build(BUILD_ID, {"dynamic_agent_role_version": 999})
    agent = RecordingAgent()
    result = execute(system, service(system, SequenceProbe(failed()), agent))
    assert result.dynamic_check_passed is False
    assert "snapshot" in result.dynamic_report_text
    assert agent.calls == []


@pytest.mark.parametrize("output", [{"retry_payload": {}}, {"report_text": "missing retry"}, {"retry_payload": {}, "report_text": 42}, {"retry_payload": "not an object", "report_text": "wrong retry shape"}, AgentExecutionError("Codex CLI did not complete successfully")])
def test_invalid_agent_output_or_execution_error_fails_without_an_unbounded_retry(system_factory, output):
    system = system_factory()
    probe, agent = SequenceProbe(failed()), RecordingAgent(output)
    result = execute(system, service(system, probe, agent))
    assert result.status == BuildStatus.FAILED and result.teardown_required is True
    assert result.dynamic_report_text
    assert len(probe.calls) == 1 and len(agent.calls) == 1


def test_secrets_are_redacted_from_saved_probes_logs_reports_and_agent_context(system_factory):
    system = system_factory(request=payload(headers={"Authorization": "Bearer header-private-secret"}))
    initial = failed(error="runtime-private-secret Bearer header-private-secret password=local-password")
    logs = RecordingLogs({"web": "APP_TOKEN=runtime-private-secret\nBearer header-private-secret"})
    agent = RecordingAgent({"retry_payload": None, "report_text": "runtime-private-secret header-private-secret password=local-password"})
    result = execute(system, service(system, SequenceProbe(initial), agent, logs))
    stored = json.dumps({"result": result.probe_result, "report": result.dynamic_report_text, "error": result.error, "context": agent.calls[0]["context"]})
    for secret in ("runtime-private-secret", "header-private-secret", "local-password"):
        assert secret not in stored
    assert "REDACTED" in stored


def test_encoded_grpc_response_cannot_leak_environment_secrets_to_results_or_agent(system_factory):
    system = system_factory()
    secret = b"runtime-private-secret"
    wire = b"\x0a" + bytes([len(secret)]) + secret
    initial = failed(response={"grpc_status": "FAILED_PRECONDITION", "body_base64": base64.b64encode(wire).decode("ascii")})
    agent = RecordingAgent()
    result = execute(system, service(system, SequenceProbe(initial), agent))
    for evidence in (result.probe_result, agent.calls[0]["context"]["probe_result"],
                     system.repository.get_route_check(system.check_id).probe_result):
        for phase in ("initial", "final"):
            assert secret not in base64.b64decode(evidence[phase]["response"]["body_base64"])


def test_retry_generated_credentials_are_redacted_before_diagnosis_is_persisted(system_factory):
    system = system_factory()
    secret = "debug-generated-private-credential"
    agent = RecordingAgent({
        "retry_payload": {"headers": {"Authorization": "Bearer " + secret}},
        "report_text": "The application requires the generated credential " + secret,
    })

    def inspect_persisted_diagnosis(count):
        if count == 2:
            saved = system.repository.get_build(BUILD_ID)
            assert secret not in saved.dynamic_report_text

    probe = SequenceProbe(failed(), passed(), before_call=inspect_persisted_diagnosis)
    result = execute(system, service(system, probe, agent))
    assert result.dynamic_check_passed is True and len(probe.calls) == 2


def test_endpoint_selection_is_bound_to_native_ids_build_ids_and_managed_present_resources(system_factory):
    system = system_factory()
    other = add_build(system, build_id="2" * 32)
    unrelated = DeploymentService(service_id="untracked-id", service_name="untracked", appid=APP_ID, build_id=BUILD_ID, image="x", status="deployed", published_ports=[PublishedPort(target_port=9000, published_port=19000)])
    system.repository.save_service(unrelated)
    build = system.repository.get_build(BUILD_ID)
    system.repository.update_build(BUILD_ID, {"service_ids": [*build.service_ids, other.service_ids[0], "untracked-id"]})
    dynamic = service(system)
    endpoints = dynamic._endpoints(system.repository.get_build(BUILD_ID), payload())
    assert endpoints == [ProbeEndpoint("web", "public.example", 18080, target_port=8000)]
    # Matching a service name alone never compensates for a missing native ID.
    manifest = system.repository.get_build_resources(BUILD_ID)
    tracked = next(iter(manifest.resources.values()))
    for patch in ({"managed": False}, {"native_id": None}, {"state": ResourceState.REMOVED}, {"state": ResourceState.PLANNED}):
        changed = tracked.model_copy(update=patch)
        system.repository.update_build_resources(BUILD_ID, {"resources": {tracked.key: changed}})
        assert dynamic._endpoints(system.repository.get_build(BUILD_ID), payload()) == []


def test_endpoint_selection_ignores_untrusted_endpoint_urls_and_non_tcp_publications(system_factory):
    system = system_factory(public_host="[::1]", public_scheme="https")
    deployment = system.repository.list_services(APP_ID)[0]
    system.repository.save_service(deployment.model_copy(update={"published_ports": [
        PublishedPort(target_port=8000, published_port=18080), PublishedPort(target_port=53, published_port=18053, protocol="udp"),
    ]}))
    endpoints = service(system)._endpoints(system.repository.get_build(BUILD_ID), payload())
    assert endpoints == [ProbeEndpoint("web", "::1", 18080, scheme="https", target_port=8000)]
    assert endpoints[0].authority == "[::1]:18080"
    assert service(system)._endpoints(system.repository.get_build(BUILD_ID), {"protocol": "grpc"})[0].scheme == "grpcs"


def test_internal_and_expose_ports_use_owned_swarm_dns_and_target_ports(system_factory):
    system = system_factory(public_host=None)
    build = system.repository.get_build(BUILD_ID)
    system.repository.update_build(BUILD_ID, {"compose_snapshot": {"services": {"web": {"expose": ["8000", "9000/tcp", "53/udp", 9090, "invalid", "0", "65536"]}}}})
    deployment = system.repository.list_services(APP_ID)[0]
    endpoints = service(system)._endpoints(system.repository.get_build(BUILD_ID), payload())
    assert [(endpoint.host, endpoint.port, endpoint.target_port) for endpoint in endpoints] == [
        (deployment.service_name, 8000, 8000), (deployment.service_name, 9000, 9000), (deployment.service_name, 9090, 9090),
    ]
    assert all(endpoint.service_name == "web" for endpoint in endpoints)
    assert build.service_ids == [deployment.service_id]


def test_manual_recheck_invalidates_old_success_resets_reservations_and_dispatches_after_lease_release(system_factory):
    system = system_factory()
    system.repository.update_build(BUILD_ID, {
        "status": BuildStatus.SUCCEEDED, "dynamic_status": "succeeded", "dynamic_check_passed": True,
        "dynamic_run_id": "old-run", "dynamic_initial_started": True, "dynamic_debug_started": True,
        "dynamic_retry_started": True, "dynamic_retry_payload": payload(), "probe_result": {"old": "result"},
    })
    role = system.container.route_checks.roles.update(DYNAMIC_ROLE, AgentRoleUpdate(prompt="Latest role for explicit recheck"))

    def assert_released():
        assert system.repository.get_build_resources(BUILD_ID).lease_owner is None

    system.dispatcher.before_dynamic = assert_released
    result = service(system).submit(APP_ID, BUILD_ID)
    assert result.status == BuildStatus.DEPLOYING and result.dynamic_status == "queued"
    assert result.dynamic_run_id != "old-run"
    assert result.dynamic_check_passed is None and result.probe_result is None
    assert result.dynamic_retry_payload is None
    assert not result.dynamic_initial_started and not result.dynamic_debug_started and not result.dynamic_retry_started
    assert result.dynamic_agent_role_version == role.agent_role_version
    assert result.route_dynamic_role_snapshot["prompt"] == role.prompt
    assert system.dispatcher.dynamic_check_runs == [(BUILD_ID, result.dynamic_run_id)]


def test_manual_recheck_rejects_a_live_build_lease_and_does_not_publish(system_factory):
    system = system_factory()
    system.repository.update_build(BUILD_ID, {"status": BuildStatus.SUCCEEDED, "dynamic_status": "succeeded"})
    with build_lease(system.repository, BUILD_ID, [CleanupStatus.NOT_STARTED]) as tracker:
        assert tracker is not None
        with pytest.raises(RouteCheckConflict, match="already running"):
            service(system).submit(APP_ID, BUILD_ID)
    assert system.dispatcher.dynamic_attempts == []
    assert system.repository.get_build(BUILD_ID).status == BuildStatus.SUCCEEDED


@pytest.mark.parametrize("patch", [
    {"status": BuildStatus.FAILED}, {"service_ids": []}, {"teardown_required": True},
])
def test_manual_recheck_rejects_unavailable_or_unstarted_deployments(system_factory, patch):
    system = system_factory()
    system.repository.update_build(BUILD_ID, patch)
    with pytest.raises(RouteCheckConflict):
        service(system).submit(APP_ID, BUILD_ID)
    assert system.dispatcher.dynamic_attempts == []


@pytest.mark.parametrize("cleanup_status", [CleanupStatus.QUEUED, CleanupStatus.RUNNING, CleanupStatus.SUCCEEDED, CleanupStatus.PARTIAL_FAILED])
def test_manual_recheck_cannot_reuse_resources_after_cleanup_has_started(system_factory, cleanup_status):
    system = system_factory()
    system.repository.update_build_resources(BUILD_ID, {"cleanup_status": cleanup_status})
    with pytest.raises(RouteCheckConflict):
        service(system).submit(APP_ID, BUILD_ID)
    assert system.dispatcher.dynamic_attempts == []


def test_manual_recheck_rejects_wrong_app_or_superseded_static_gate(system_factory):
    system = system_factory()
    with pytest.raises(NotFoundError):
        service(system).submit("other-app", BUILD_ID)
    system.repository.update_route_check(system.check_id, {"generation": 2})
    with pytest.raises(RouteCheckInputError):
        service(system).submit(APP_ID, BUILD_ID)
    assert system.dispatcher.dynamic_attempts == []


def test_broker_failure_preserves_a_queued_manual_run_and_recovery_republishes_same_identity(system_factory):
    system = system_factory()
    dynamic = service(system, SequenceProbe(passed()))
    system.dispatcher.offline = True
    with pytest.raises(RouteCheckUnavailableError) as error:
        dynamic.submit(APP_ID, BUILD_ID)
    assert "broker-private" not in str(error.value)
    queued = system.repository.get_build(BUILD_ID)
    assert queued.dynamic_status == "queued" and queued.status == BuildStatus.DEPLOYING
    assert queued.dynamic_dispatch_error == "Unable to enqueue dynamic check"
    assert queued.dynamic_run_id
    assert system.repository.get_build_resources(BUILD_ID).lease_owner is None
    assert dynamic.recover()["dynamic_checks_scheduled"] == 0
    system.dispatcher.offline = False
    assert dynamic.recover() == {"dynamic_checks_scheduled": 1, "teardowns_recovered": 0}
    assert system.dispatcher.dynamic_check_runs == [(BUILD_ID, queued.dynamic_run_id)]
    result = execute(system, dynamic)
    assert result.dynamic_check_passed is True and result.dynamic_dispatch_error is None


def test_recovery_skips_a_live_lease_but_recovers_an_expired_naive_utc_lease(system_factory):
    system = system_factory()
    dynamic = service(system)
    system.repository.update_build(BUILD_ID, {"dynamic_status": "queued", "dynamic_run_id": "saved-run"})
    with build_lease(system.repository, BUILD_ID, [CleanupStatus.NOT_STARTED]) as tracker:
        assert tracker is not None
        assert dynamic.recover()["dynamic_checks_scheduled"] == 0
    system.repository.update_build_resources(BUILD_ID, {"lease_owner": "dead-worker", "lease_expires_at": (utc_now() - timedelta(seconds=1)).replace(tzinfo=None)})
    assert dynamic.recover()["dynamic_checks_scheduled"] == 1
    assert system.dispatcher.dynamic_check_runs == [(BUILD_ID, "saved-run")]


def test_build_specific_results_survive_another_build_claiming_the_shared_check(system_factory):
    system = system_factory()
    other_id = "2" * 32
    add_build(system, build_id=other_id)
    other_service = service(system, SequenceProbe(passed(response={"status_code": 200, "body": "other build success"})))

    def initial_for_first_build():
        other = execute(system, other_service, other_id)
        assert other.dynamic_check_passed is True
        return failed(response={"status_code": 503, "body": "first build failure"})

    first_service = service(system, SequenceProbe(initial_for_first_build), RecordingAgent())
    first = execute(system, first_service)
    second = system.repository.get_build(other_id)
    aggregate = system.repository.get_route_check(system.check_id)
    assert first.dynamic_check_passed is False and first.status == BuildStatus.FAILED
    assert second.dynamic_check_passed is True and second.probe_result["final"]["response"]["body"] == "other build success"
    assert first.probe_result["initial"]["response"]["body"] == "first build failure"
    assert aggregate.dynamic_build_id == other_id
    assert aggregate.dynamic_check_passed is True and aggregate.probe_result == second.probe_result


@pytest.mark.parametrize("manual", [False, True])
def test_shared_aggregate_cas_contention_does_not_fail_a_valid_deployment(system_factory, monkeypatch, manual):
    system = system_factory()
    other_id = "2" * 32
    add_build(system, build_id=other_id)
    other = service(system, SequenceProbe(passed(response={"body": "peer success"})))
    original_update = system.repository.update_route_check
    contended = False

    def peer_claims_before_cas(check_id, fields, expected=None):
        nonlocal contended
        if not contended and fields.get("dynamic_build_id") == BUILD_ID:
            contended = True
            execute(system, other, other_id)
        return original_update(check_id, fields, expected)

    monkeypatch.setattr(system.repository, "update_route_check", peer_claims_before_cas)
    probe = SequenceProbe(passed(response={"body": "this build success"}))
    dynamic = service(system, probe)
    if manual:
        dynamic.submit(APP_ID, BUILD_ID)
        assert len(system.dispatcher.dynamic_check_runs) == 1
    result = execute(system, dynamic)
    assert result.dynamic_check_passed is True and not result.teardown_required
    assert len(probe.calls) == 1
    aggregate = system.repository.get_route_check(system.check_id)
    assert aggregate.dynamic_build_id == other_id and aggregate.dynamic_check_passed is True
    assert aggregate.probe_result == system.repository.get_build(other_id).probe_result


@pytest.mark.parametrize("change", ["force", "delete"])
def test_static_gate_change_during_result_claim_blocks_probe_io(system_factory, monkeypatch, change):
    system = system_factory()
    original_update = system.repository.update_route_check
    changed = False

    def supersede_before_claim(check_id, fields, expected=None):
        nonlocal changed
        if not changed and fields.get("dynamic_build_id") == BUILD_ID:
            changed = True
            if change == "force":
                original_update(check_id, {"generation": 2})
            else:
                assert system.repository.delete_route_check(APP_ID, COMMIT, "/core", 1)
        return original_update(check_id, fields, expected)

    monkeypatch.setattr(system.repository, "update_route_check", supersede_before_claim)
    probe = SequenceProbe()
    result = execute(system, service(system, probe))
    assert result.dynamic_check_passed is False and result.teardown_required
    assert probe.calls == []
    check = system.repository.get_route_check(system.check_id)
    if change == "delete":
        assert check is None
    else:
        assert check.generation == 2 and check.dynamic_build_id is None


def test_resuming_an_older_run_does_not_reclaim_a_newer_builds_aggregate(system_factory):
    system = system_factory()
    other_id = "2" * 32
    add_build(system, build_id=other_id)
    probe = SequenceProbe(WorkerCrash())
    dynamic = service(system, probe)
    with pytest.raises(WorkerCrash):
        execute(system, dynamic)
    peer = execute(system, service(system, SequenceProbe(passed())), other_id)
    result = execute(system, dynamic)
    assert result.dynamic_check_passed is False and result.teardown_required
    aggregate = system.repository.get_route_check(system.check_id)
    assert aggregate.dynamic_build_id == other_id and aggregate.dynamic_check_passed is True
    assert aggregate.probe_result == peer.probe_result
    assert len(probe.calls) == 1


@pytest.mark.parametrize("change", ["force", "delete"])
def test_in_flight_old_dynamic_result_cannot_pollute_a_forced_or_deleted_static_result(system_factory, change):
    system = system_factory()

    def change_static_generation():
        if change == "force":
            submission = system.container.route_checks.create_or_reuse(system.app, COMMIT, force=True)
            assert submission.result.generation == 2
        else:
            assert system.container.route_checks.delete_latest(APP_ID, COMMIT) is True
        return passed()

    execute(system, service(system, SequenceProbe(change_static_generation)))
    check = system.repository.get_route_check(system.check_id)
    if change == "delete":
        assert check is None
    else:
        assert check.generation == 2 and check.static_status == "queued"
        assert check.dynamic_status == "pending" and check.probe_result is None
        assert check.dynamic_build_id is None


def test_old_build_cannot_start_a_probe_using_a_new_static_generation(system_factory):
    system = system_factory()
    system.container.route_checks.create_or_reuse(system.app, COMMIT, force=True)
    probe = SequenceProbe()
    result = execute(system, service(system, probe))
    assert result.dynamic_check_passed is False and result.teardown_required is True
    assert probe.calls == []
    current = system.repository.get_route_check(system.check_id)
    assert current.generation == 2 and current.probe_result is None and current.dynamic_build_id is None


def test_deleted_then_recreated_check_is_not_resurrected_or_overwritten_by_an_old_build(system_factory):
    system = system_factory()
    old_id = system.check_id
    assert system.container.route_checks.delete_latest(APP_ID, COMMIT)
    fresh = system.container.route_checks.create_or_reuse(system.app, COMMIT)
    assert fresh.check_id != old_id
    result = execute(system, service(system, SequenceProbe()))
    assert result.dynamic_check_passed is False
    assert system.repository.get_route_check(old_id) is None
    assert system.repository.get_route_check(fresh.check_id).model_dump() == fresh.result.model_dump()


class ControlledCleaner:
    """A cleanup-adapter double; the real runner owns order, leases and state."""

    def __init__(self, repository, *, fail_once=False):
        self.repository = repository
        self.fail_once = fail_once
        self.removed = []
        self.discovery_builds = []

    def discover(self, manifest, tracker):
        tracker.guard()
        self.discovery_builds.append(manifest.build_id)

    def wait_containers_stopped(self, manifest, tracker):
        tracker.guard()

    def remove(self, manifest, resource, tracker):
        tracker.guard()
        if self.fail_once:
            self.fail_once = False
            raise ResourceConflict("Controlled Docker endpoint temporarily unavailable")
        self.removed.append((manifest.build_id, resource.kind, resource.name))

    def verify_empty(self, manifest, tracker):
        tracker.guard()
        if any(resource.state != ResourceState.REMOVED for resource in manifest.resources.values()):
            raise ResourceConflict("Controlled build runtime still has resources")


def cleanup_runner(system, cleaner):
    return BuildCleanupRunner(system.settings, system.container.build_resources, docker_factory=lambda: cleaner)


@pytest.mark.parametrize("interrupt_at", ["build_failure", "aggregate_sync"])
def test_recovery_completes_interrupted_failure_transition_before_teardown(system_factory, monkeypatch, interrupt_at):
    system = system_factory()
    probe, agent, logs = SequenceProbe(failed()), RecordingAgent(), RecordingLogs()
    dynamic = service(system, probe, agent, logs)
    original_fail = system.container.builds.fail
    original_sync = dynamic._sync_result

    def crash_before_build_failure(*_args, **_kwargs):
        raise WorkerCrash()

    if interrupt_at == "build_failure":
        monkeypatch.setattr(system.container.builds, "fail", crash_before_build_failure)
    else:
        def crash_before_aggregate_sync(build, guard):
            if build.dynamic_status == "failed":
                raise WorkerCrash()
            return original_sync(build, guard)
        monkeypatch.setattr(dynamic, "_sync_result", crash_before_aggregate_sync)
    with pytest.raises(WorkerCrash):
        execute(system, dynamic)
    interrupted = system.repository.get_build(BUILD_ID)
    assert interrupted.status == (BuildStatus.DEPLOYING if interrupt_at == "build_failure" else BuildStatus.FAILED)
    assert interrupted.dynamic_status == "failed"
    assert interrupted.teardown_required
    monkeypatch.setattr(system.container.builds, "fail", original_fail)
    monkeypatch.setattr(dynamic, "_sync_result", original_sync)
    cleaner = ControlledCleaner(system.repository)
    runner = cleanup_runner(system, cleaner)
    monkeypatch.setattr("orchestrator.dynamic_checks.BuildCleanupRunner", lambda *_args, **_kwargs: runner)
    assert dynamic.recover()["teardowns_recovered"] == 1
    result = system.repository.get_build(BUILD_ID)
    assert result.status == BuildStatus.FAILED and result.dynamic_check_passed is False
    assert result.teardown_status == "succeeded" and result.teardown_required is False
    check = system.repository.get_route_check(system.check_id)
    assert check.dynamic_status == "failed" and check.dynamic_check_passed is False
    assert check.dynamic_report_text == result.dynamic_report_text
    assert len(probe.calls) == len(agent.calls) == len(logs.calls) == 1
    assert len(system.repository.list_alerts(APP_ID)) == 1
    assert cleaner.removed


def test_failure_teardown_runs_after_lease_release_without_a_broker_and_only_cleans_its_build(system_factory):
    system = system_factory()
    other_id = "2" * 32
    add_build(system, build_id=other_id)
    tracker = ResourceTracker(system.repository, BUILD_ID)
    tracker.record("network", "build-private-network", target="docker:controlled-engine", native_id="network-one", state=ResourceState.PRESENT)
    dynamic = service(system, SequenceProbe(failed()), RecordingAgent())
    cleaner = ControlledCleaner(system.repository)
    system.dispatcher.offline = True
    with build_lease(system.repository, BUILD_ID, [CleanupStatus.NOT_STARTED]) as active:
        assert active is not None
        result = dynamic.execute(BUILD_ID, guard=active.guard)
        assert result.status == BuildStatus.FAILED
        # The active deployment lease must prevent any premature deletion.
        assert dynamic.teardown(BUILD_ID, cleanup=cleanup_runner(system, cleaner).run)["teardown_status"] == "partial_failed"
        assert cleaner.removed == []
    assert system.repository.get_build_resources(BUILD_ID).lease_owner is None
    result = dynamic.teardown(BUILD_ID, cleanup=cleanup_runner(system, cleaner).run)
    assert result == {"build_id": BUILD_ID, "teardown_status": "succeeded"}
    final = system.repository.get_build(BUILD_ID)
    assert final.status == BuildStatus.FAILED and final.dynamic_check_passed is False
    assert final.teardown_required is False and final.teardown_status == "succeeded"
    assert [kind for build_id, kind, _name in cleaner.removed if build_id == BUILD_ID] == ["service", "network"]
    assert all(resource.state == ResourceState.REMOVED for resource in system.repository.get_build_resources(BUILD_ID).resources.values())
    assert all(resource.state == ResourceState.PRESENT for resource in system.repository.get_build_resources(other_id).resources.values())
    assert all(build_id == BUILD_ID for build_id in cleaner.discovery_builds)
    assert system.dispatcher.cleanup_attempts == []


def test_partial_teardown_is_durable_and_recovered_locally_even_with_broker_offline(system_factory, monkeypatch):
    system = system_factory()
    dynamic = service(system, SequenceProbe(failed()))
    execute(system, dynamic)
    cleaner = ControlledCleaner(system.repository, fail_once=True)
    monkeypatch.setattr("orchestrator.dynamic_checks.BuildCleanupRunner", lambda _settings, _resources: cleanup_runner(system, cleaner))
    system.dispatcher.offline = True
    diagnosis = system.repository.get_build(BUILD_ID).dynamic_report_text
    assert dynamic.teardown(BUILD_ID)["teardown_status"] == "partial_failed"
    saved = system.repository.get_build(BUILD_ID)
    assert saved.teardown_required is True and saved.dynamic_report_text == diagnosis
    assert system.repository.get_build_resources(BUILD_ID).cleanup_status == CleanupStatus.PARTIAL_FAILED
    assert dynamic.recover() == {"dynamic_checks_scheduled": 0, "teardowns_recovered": 1}
    recovered = system.repository.get_build(BUILD_ID)
    assert recovered.teardown_status == "succeeded" and recovered.teardown_required is False
    assert recovered.dynamic_report_text == diagnosis and recovered.status == BuildStatus.FAILED
    assert system.dispatcher.cleanup_attempts == []


def test_teardown_without_a_manifest_is_diagnosable_and_keeps_the_recovery_marker(system_factory):
    system = system_factory()
    system.repository.update_build(BUILD_ID, {"teardown_required": True, "teardown_status": "pending", "status": BuildStatus.FAILED})
    system.repository.build_resources.pop(BUILD_ID)
    result = service(system).sync_teardown(BUILD_ID)
    assert result["teardown_status"] == "partial_failed"
    assert system.repository.get_build(BUILD_ID).teardown_required is True


def test_stale_worker_message_cannot_execute_a_new_manual_recheck(system_factory):
    system = system_factory()
    probe = SequenceProbe(passed())
    dynamic = service(system, probe)
    queued = dynamic.submit(APP_ID, BUILD_ID)
    worker = runtime(system, dynamic)
    ignored = worker.dynamic_route_check(BUILD_ID, "older-run")
    assert ignored["status"] == BuildStatus.DEPLOYING.value
    assert probe.calls == []
    assert system.repository.get_build(BUILD_ID).dynamic_run_id == queued.dynamic_run_id
    delivered = worker.dynamic_route_check(BUILD_ID, queued.dynamic_run_id)
    assert delivered["status"] == BuildStatus.SUCCEEDED.value
    assert len(probe.calls) == 1
    worker.dynamic_route_check(BUILD_ID, queued.dynamic_run_id)
    assert len(probe.calls) == 1


@pytest.fixture
def local_http():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            self.server.requests.append((self.path, data))
            ready = self.path == "/core" and data.get("phase") == "ready"
            body = json.dumps({"ok": ready, "phase": data.get("phase")}).encode()
            self.send_response(200 if ready else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def point_at_port(system, port):
    deployment = next(item for item in system.repository.list_services(APP_ID) if item.build_id == BUILD_ID)
    system.repository.save_service(deployment.model_copy(update={"published_ports": [PublishedPort(target_port=8000, published_port=port)]}))


def test_real_http_from_build_frozen_payload_through_debug_retry_and_worker_delivery(system_factory, local_http):
    system = system_factory(public_host="127.0.0.1", request=payload(assertions={"expected_status_list": [200], "json_contains": {"ok": True}}))
    point_at_port(system, local_http.server_port)
    agent = RecordingAgent({"retry_payload": {"body": {"phase": "ready"}}, "report_text": "Runtime needs phase=ready"})
    logs = RecordingLogs()
    dynamic = service(system, ProbeClient(timeout_seconds=2), agent, logs)
    result = runtime(system, dynamic).dynamic_route_check(BUILD_ID)
    assert result["status"] == BuildStatus.SUCCEEDED.value
    build = system.repository.get_build(BUILD_ID)
    assert build.probe_result["initial"]["response"]["status_code"] == 503
    assert build.probe_result["retry"]["response"]["status_code"] == 200
    assert build.probe_result["final"]["response"]["json"] == {"ok": True, "phase": "ready"}
    assert local_http.requests == [("/core", {"phase": "initial"}), ("/core", {"phase": "ready"})]
    assert len(agent.calls) == 1 and logs.calls == [BUILD_ID]
    assert system.repository.get_route_check(system.check_id).probe_result == build.probe_result


def test_real_http_retry_failure_is_failed_and_cleaned_by_worker_without_redis(system_factory, local_http, monkeypatch):
    system = system_factory(public_host="127.0.0.1")
    point_at_port(system, local_http.server_port)
    agent = RecordingAgent({"retry_payload": {"body": {"phase": "still-wrong"}}, "report_text": "Runtime payload cannot satisfy target route"})
    dynamic = service(system, ProbeClient(timeout_seconds=2), agent)
    worker = runtime(system, dynamic)
    cleaner = ControlledCleaner(system.repository)
    monkeypatch.setattr(worker, "cleanup_build", cleanup_runner(system, cleaner).run)
    system.dispatcher.offline = True
    result = worker.dynamic_route_check(BUILD_ID)
    assert result["status"] == BuildStatus.FAILED.value
    build = system.repository.get_build(BUILD_ID)
    assert build.dynamic_check_passed is False
    assert build.teardown_status == "succeeded" and build.teardown_required is False
    assert len(local_http.requests) == 2 and len(agent.calls) == 1
    assert system.dispatcher.cleanup_attempts == []
    assert system.repository.get_build_resources(BUILD_ID).cleanup_status == CleanupStatus.SUCCEEDED
    assert cleaner.removed


@pytest.fixture
def local_grpc():
    descriptor = descriptor_pb2.FileDescriptorProto(name="dynamic_route.proto", package="dynamicchecks", syntax="proto3")
    request = descriptor.message_type.add(name="Request")
    request.field.add(name="phase", number=1, type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    response = descriptor.message_type.add(name="Response")
    response.field.add(name="ready", number=1, type=descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)
    descriptor.service.add(name="Route").method.add(name="Check", input_type=".dynamicchecks.Request", output_type=".dynamicchecks.Response")
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(descriptor.SerializeToString())
    request_class = message_factory.GetMessageClass(pool.FindMessageTypeByName("dynamicchecks.Request"))
    response_class = message_factory.GetMessageClass(pool.FindMessageTypeByName("dynamicchecks.Response"))
    descriptor_set = descriptor_pb2.FileDescriptorSet()
    descriptor_set.file.add().CopyFrom(descriptor)
    calls = []

    def check(request, context):
        calls.append(request.phase)
        if request.phase != "ready":
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "phase must be ready")
        return response_class(ready=True)

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    handler = grpc.unary_unary_rpc_method_handler(check, request_deserializer=request_class.FromString, response_serializer=response_class.SerializeToString)
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("dynamicchecks.Route", {"Check": handler}),))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield SimpleNamespace(port=port, calls=calls, descriptor_set_base64=base64.b64encode(descriptor_set.SerializeToString()).decode("ascii"), response_class=response_class)
    finally:
        server.stop(0).wait(timeout=2)


def test_real_grpc_build_gate_payload_debug_retry_and_worker_delivery(system_factory, local_grpc):
    request = {
        "protocol": "grpc", "body": {"phase": "initial"}, "descriptor_set_base64": local_grpc.descriptor_set_base64,
        "assertions": {"expected_grpc_status_list": ["OK"], "json_equals": {"ready": True}},
    }
    system = system_factory(public_host="127.0.0.1", route="/dynamicchecks.Route/Check", request=request)
    point_at_port(system, local_grpc.port)
    agent = RecordingAgent({"retry_payload": {"body": {"phase": "ready"}}, "report_text": "gRPC application requires phase=ready"})
    dynamic = service(system, ProbeClient(timeout_seconds=2), agent)
    result = runtime(system, dynamic).dynamic_route_check(BUILD_ID)
    assert result["status"] == BuildStatus.SUCCEEDED.value
    build = system.repository.get_build(BUILD_ID)
    assert build.dynamic_check_passed is True
    assert build.probe_result["initial"]["response"]["grpc_status"] == "FAILED_PRECONDITION"
    assert build.probe_result["retry"]["response"]["grpc_status"] == "OK"
    assert build.probe_result["final"]["response"]["json"] == {"ready": True}
    wire = base64.b64decode(build.probe_result["final"]["response"]["body_base64"])
    assert local_grpc.response_class.FromString(wire).ready is True
    assert local_grpc.calls == ["initial", "ready"] and len(agent.calls) == 1
    assert system.repository.get_route_check(system.check_id).probe_result == build.probe_result


def test_grpc_retry_cannot_reinterpret_unchanged_response_bytes_with_replacement_descriptors(system_factory):
    """Changing output field numbers changes success criteria, not a payload.

    The real server always emits ``shadow=true`` and never ``ready=true``. A
    replacement descriptor maps the same wire field to ``ready``. Assertions
    remain byte-for-byte equal, but accepting this descriptor would bypass the
    original JSON quality gate without changing server behavior.
    """

    def contract(*, swap_fields):
        descriptor = descriptor_pb2.FileDescriptorProto(name="frozen_output.proto", package="frozenoutput", syntax="proto3")
        descriptor.message_type.add(name="Request")
        response = descriptor.message_type.add(name="Response")
        response.field.add(name="ready", number=2 if swap_fields else 1, type=descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)
        response.field.add(name="shadow", number=1 if swap_fields else 2, type=descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)
        descriptor.service.add(name="Route").method.add(name="Check", input_type=".frozenoutput.Request", output_type=".frozenoutput.Response")
        pool = descriptor_pool.DescriptorPool()
        pool.AddSerializedFile(descriptor.SerializeToString())
        descriptor_set = descriptor_pb2.FileDescriptorSet()
        descriptor_set.file.add().CopyFrom(descriptor)
        return SimpleNamespace(
            request=message_factory.GetMessageClass(pool.FindMessageTypeByName("frozenoutput.Request")),
            response=message_factory.GetMessageClass(pool.FindMessageTypeByName("frozenoutput.Response")),
            encoded=base64.b64encode(descriptor_set.SerializeToString()).decode("ascii"),
        )

    original, replacement = contract(swap_fields=False), contract(swap_fields=True)
    calls = []

    def check(request, _context):
        calls.append(request.SerializeToString())
        return original.response(shadow=True)

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    handler = grpc.unary_unary_rpc_method_handler(check, request_deserializer=original.request.FromString, response_serializer=original.response.SerializeToString)
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("frozenoutput.Route", {"Check": handler}),))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        request = {
            "protocol": "grpc", "body": {}, "descriptor_set_base64": original.encoded,
            "assertions": {"expected_grpc_status_list": ["OK"], "json_contains": {"ready": True}},
        }
        system = system_factory(public_host="127.0.0.1", route="/frozenoutput.Route/Check", request=request)
        point_at_port(system, port)
        agent = RecordingAgent({"retry_payload": {"descriptor_set_base64": replacement.encoded}, "report_text": "A descriptor must not redefine the meaning of the output assertions"})
        dynamic = service(system, ProbeClient(timeout_seconds=2), agent)
        result = execute(system, dynamic)
        assert result.dynamic_check_passed is False
        assert result.teardown_required is True
        assert result.dynamic_retry_started is False
        assert len(calls) == 1
    finally:
        server.stop(0).wait(timeout=2)
