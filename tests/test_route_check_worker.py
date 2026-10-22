"""Worker/service integration for the static and dynamic delivery gates.

All durable services and leases are real. Only external Jenkins, Docker,
Codex, logs and network probes are replaced with bounded, recorded fakes.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from orchestrator.build_resources import BuildCleanupRunner, build_lease
from orchestrator.dynamic_checks import DynamicCheckService
from orchestrator.docker_services import DockerServiceError
from orchestrator.jenkins import JenkinsBuild, JenkinsQueueItem
from orchestrator.models import AgentRoleUpdate, BuildStatus, CleanupStatus, ResourceState, utc_now
from orchestrator.route_checks import BUILD_GATE_ERROR, STATIC_ROLE
from orchestrator.worker_runtime import WorkerRuntime
from test_worker_runtime import (
    FakeDockerServices, FakeJenkins, make_harness, queue_build,
    successful_artifact, successful_deployment,
)


class OwnedDocker(FakeDockerServices):
    def deploy_build(self, appid, build_id, compose, tracker):
        deployment = super().deploy_build(appid, build_id, compose, tracker)
        for service in deployment.services:
            tracker.record("service", service.service_name, native_id=service.service_id,
                state=ResourceState.PRESENT, metadata={"compose_service": "api"})
        return deployment


class RecordedProbe:
    def __init__(self, results, *, on_run=None):
        self.results = list(results)
        self.calls = []
        self.on_run = on_run

    def run(self, payload, *, require_route, endpoints):
        self.calls.append((payload, require_route, endpoints))
        assert endpoints, "A route probe must have a persisted build-owned endpoint"
        if self.on_run:
            self.on_run()
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class RecordedDebug:
    def __init__(self, output=None):
        self.output = output if output is not None else {
            "retry_payload": {"method": "POST", "body": {"ready": True}},
            "report_text": "路由需要 POST JSON 请求",
        }
        self.calls = []

    def run_json(self, role, context, *, cwd, **kwargs):
        self.calls.append((role, context, cwd))
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


class RecordedLogs:
    def __init__(self):
        self.calls = []

    def collect(self, build_id):
        self.calls.append(build_id)
        return {"services": [{"name": "api", "logs": "request method GET rejected"}]}


def pipeline():
    jenkins = FakeJenkins(
        [JenkinsQueueItem("https://jenkins.example/queue/item/42/", 42, build_number=42)],
        build=JenkinsBuild(42, False, "SUCCESS"), artifact=successful_artifact(),
    )
    docker = OwnedDocker(successful_deployment())
    harness = make_harness(jenkins=jenkins, docker_services=docker)
    return harness, queue_build(harness), jenkins, docker


def install_dynamic(harness, results, *, debug_output=None, on_probe=None):
    probe = RecordedProbe(results, on_run=on_probe)
    debug = RecordedDebug(debug_output)
    logs = RecordedLogs()
    service = DynamicCheckService(
        harness.container.repository, harness.container.dispatcher, harness.container.settings,
        harness.container.route_checks, harness.container.builds, harness.container.build_resources,
        probe_client=probe, executor=debug, log_collector=logs,
    )
    harness.runtime._dynamic_checks = service
    return service, probe, debug, logs


def record_existing_deployment(harness, build):
    for status in (BuildStatus.VALIDATING, BuildStatus.TRIGGERING, BuildStatus.BUILDING, BuildStatus.DEPLOYING):
        harness.container.builds.transition(build.build_id, status)
    records = successful_deployment().to_models(build.build_id)
    with build_lease(harness.container.repository, build.build_id, [CleanupStatus.NOT_STARTED]) as tracker:
        assert tracker is not None
        for record in records:
            harness.container.repository.save_service(record)
            tracker.record("service", record.service_name, native_id=record.service_id,
                state=ResourceState.PRESENT, metadata={"compose_service": "api"})
    return harness.container.repository.update_build(build.build_id, {
        "service_ids": [record.service_id for record in records],
    })


def local_cleanup(harness, monkeypatch, *, failure=False):
    calls = []

    def cleanup(build_id):
        manifest = harness.container.repository.get_build_resources(build_id)
        assert manifest.lease_owner is None
        assert manifest.cleanup_status is CleanupStatus.QUEUED
        assert harness.runtime._operation_tracker.get() is None
        assert harness.container.repository.get_build(build_id).status is BuildStatus.FAILED
        calls.append(build_id)
        if failure:
            raise RuntimeError("private Docker endpoint failure")
        harness.container.repository.update_build_resources(build_id, {"cleanup_status": CleanupStatus.SUCCEEDED})
        return {"build_id": build_id, "cleanup_status": "succeeded"}

    monkeypatch.setattr(harness.runtime, "cleanup_build", cleanup)
    return calls


@pytest.mark.parametrize("changed", [
    "route", "missing-route", "role-version", "deleted-check", "forced-generation", "missing-generation", "missing-service",
])
@pytest.mark.parametrize("starting_status", ["queued", "triggering"])
def test_worker_revalidates_static_gate_immediately_before_jenkins(changed, starting_status):
    harness, build, jenkins, _ = pipeline()
    repository = harness.container.repository
    if starting_status == "triggering":
        harness.container.builds.transition(build.build_id, BuildStatus.VALIDATING)
        harness.container.builds.transition(build.build_id, BuildStatus.TRIGGERING)
    if changed in {"route", "missing-route"}:
        repository.update_app(build.appid, {"require_route": "/changed" if changed == "route" else None})
    elif changed == "role-version":
        harness.container.route_checks.roles.update(STATIC_ROLE, AgentRoleUpdate(prompt="new static policy"))
    elif changed == "deleted-check":
        assert harness.container.route_checks.delete_latest(build.appid, build.git_commit_sha)
    elif changed == "forced-generation":
        app = harness.container.applications.get_record(build.appid)
        forced = harness.container.route_checks.create_or_reuse(app, build.git_commit_sha, force=True)
        assert forced.result.generation == build.route_check_generation + 1
        repository.update_route_check(forced.check_id, {"static_check_passed": True, "static_status": "succeeded"})
    elif changed == "missing-generation":
        repository.update_build(build.build_id, {"route_check_generation": None})
    else:
        harness.runtime._route_checks = None
        harness.container.builds.route_checks = None

    assert harness.runtime.start_build(build.build_id, "worker") == {"build_id": build.build_id, "status": "failed"}
    stored = repository.get_build(build.build_id)
    assert stored.error == BUILD_GATE_ERROR
    assert not stored.teardown_required
    assert jenkins.requests == []
    assert harness.scheduled_polls == []
    assert not repository.get_build_resources(build.build_id).jenkins_trigger_pending


def test_gate_reads_current_app_even_if_configuration_changes_during_preflight(monkeypatch):
    harness, build, jenkins, _ = pipeline()

    def client():
        harness.container.repository.update_app(build.appid, {"require_route": "/changed-after-validation"})
        return jenkins

    monkeypatch.setattr(harness.runtime, "_jenkins_adapter", client)
    assert harness.runtime.start_build(build.build_id, "worker")["status"] == "failed"
    assert harness.container.repository.get_build(build.build_id).error == BUILD_GATE_ERROR
    assert not jenkins.requests


def test_static_task_forwards_identifier_and_generation_and_recovery_merges_services(monkeypatch):
    harness = make_harness()
    calls = []

    def execute(check_id, generation=None):
        calls.append((check_id, generation))
        return {"check_id": check_id, "status": "superseded"}

    monkeypatch.setattr(harness.container.route_checks, "execute_static", execute)
    monkeypatch.setattr(harness.container.route_checks, "recover", lambda: {"static_checks_scheduled": 3})
    monkeypatch.setattr(harness.dynamic_checks, "recover", lambda: {"dynamic_checks_scheduled": 2, "teardowns_recovered": 1})
    assert harness.runtime.static_route_check("check", 7) == {"check_id": "check", "status": "superseded"}
    assert calls == [("check", 7)]
    assert harness.runtime.recover_route_checks() == {
        "static_checks_scheduled": 3, "dynamic_checks_scheduled": 2, "teardowns_recovered": 1,
    }


def test_initial_probe_runs_after_records_are_persisted_but_before_build_success():
    harness, build, _, _ = pipeline()

    def assert_not_delivered():
        current = harness.container.repository.get_build(build.build_id)
        assert current.status is BuildStatus.DEPLOYING
        assert current.service_ids == ["service-demo-api"]
        assert harness.container.repository.list_services(build.appid)[0].build_id == build.build_id

    _, probe, debug, logs = install_dynamic(harness, [
        {"passed": True, "status_code": 200, "body": "ready response"},
    ], on_probe=assert_not_delivered)
    assert harness.runtime.start_build(build.build_id, "start")["status"] == "building"
    assert harness.runtime.poll_build(build.build_id, 0, "poll")["status"] == "succeeded"
    current = harness.container.repository.get_build(build.build_id)
    check = harness.container.repository.get_route_check(build.route_check_id)
    assert current.dynamic_check_passed is True and check.dynamic_check_passed is True
    assert current.probe_result["final"]["body"] == "ready response"
    assert check.probe_result == current.probe_result
    assert len(probe.calls) == 1
    assert not debug.calls and not logs.calls
    assert not current.teardown_required


def test_debug_retry_success_preserves_final_response_and_never_tears_down(monkeypatch):
    harness, build, _, _ = pipeline()
    _, probe, debug, logs = install_dynamic(harness, [
        {"passed": False, "status_code": 405}, {"passed": True, "status_code": 200, "body": "fixed request"},
    ])
    teardown = []
    monkeypatch.setattr(harness.runtime.dynamic_checks, "teardown", lambda *a, **kw: teardown.append(a))
    harness.runtime.start_build(build.build_id, "start")
    assert harness.runtime.poll_build(build.build_id, 0, "poll")["status"] == "succeeded"
    current = harness.container.repository.get_build(build.build_id)
    assert current.dynamic_check_passed is True and current.dynamic_report_text is None
    assert current.probe_result["initial"]["status_code"] == 405
    assert current.probe_result["retry"] == current.probe_result["final"]
    assert current.probe_result["final"]["body"] == "fixed request"
    assert len(probe.calls) == 2 and len(debug.calls) == 1 and logs.calls == [build.build_id]
    assert probe.calls[1][0]["method"] == "POST"
    assert not teardown


def test_failed_retry_blocks_delivery_and_cleans_immediately_after_releasing_lease(monkeypatch):
    harness, build, _, _ = pipeline()
    _, probe, debug, logs = install_dynamic(harness, [
        {"passed": False, "status_code": 405}, {"passed": False, "status_code": 500, "body": "still failing"},
    ])
    cleanup = local_cleanup(harness, monkeypatch)

    def broker_must_not_be_used(*args, **kwargs):
        pytest.fail("Immediate quality-gate teardown must not depend on a cleanup broker message")

    monkeypatch.setattr(harness.container.dispatcher, "dispatch_build_cleanup", broker_must_not_be_used)
    harness.runtime.start_build(build.build_id, "start")
    assert harness.runtime.poll_build(build.build_id, 0, "poll")["status"] == "failed"
    current = harness.container.repository.get_build(build.build_id)
    check = harness.container.repository.get_route_check(build.route_check_id)
    assert current.status is BuildStatus.FAILED and current.dynamic_check_passed is False
    assert current.teardown_status == "succeeded" and current.teardown_required is False
    assert "唯一一次探针重试" in current.dynamic_report_text
    assert check.dynamic_report_text == current.dynamic_report_text
    assert check.dynamic_check_passed is False
    assert current.probe_result["final"]["body"] == "still failing"
    assert len(probe.calls) == 2 and len(debug.calls) == 1 and logs.calls == [build.build_id]
    assert cleanup == [build.build_id]
    assert not any(event.kind == "build.deployed" for event in harness.container.repository.list_events(build.appid))
    # A delayed duplicate cannot probe again or revive a failed delivery.
    assert harness.runtime.poll_build(build.build_id, 1, "duplicate")["status"] == "failed"
    assert len(probe.calls) == 2 and cleanup == [build.build_id]


def test_teardown_failure_remains_durable_and_a_later_terminal_task_retries_cleanup(monkeypatch):
    harness, build, _, _ = pipeline()
    _, probe, debug, _ = install_dynamic(harness, [{"passed": False, "status_code": 404}],
        debug_output={"retry_payload": None, "report_text": "目标路由不存在"})
    failed_cleanup = local_cleanup(harness, monkeypatch, failure=True)
    harness.runtime.start_build(build.build_id, "start")
    assert harness.runtime.poll_build(build.build_id, 0, "poll")["status"] == "failed"
    current = harness.container.repository.get_build(build.build_id)
    assert current.teardown_required and current.teardown_status == "partial_failed"
    assert current.dynamic_report_text == "目标路由不存在"
    assert "private" not in current.error
    assert failed_cleanup == [build.build_id]
    recovered_cleanup = local_cleanup(harness, monkeypatch)
    assert harness.runtime.start_build(build.build_id, "delayed")["status"] == "failed"
    assert recovered_cleanup == [build.build_id]
    assert harness.container.repository.get_build(build.build_id).teardown_status == "succeeded"
    assert len(probe.calls) == 1 and len(debug.calls) == 1


def test_unexpected_dynamic_exception_is_fail_closed_and_still_cleans(monkeypatch):
    harness, build, _, _ = pipeline()
    service, _, _, _ = install_dynamic(harness, [])

    def fail(*args, **kwargs):
        raise RuntimeError("private agent transport detail")

    monkeypatch.setattr(service, "execute", fail)
    cleanup = local_cleanup(harness, monkeypatch)
    harness.runtime.start_build(build.build_id, "start")
    assert harness.runtime.poll_build(build.build_id, 0, "poll")["status"] == "failed"
    current = harness.container.repository.get_build(build.build_id)
    assert current.dynamic_check_passed is False and current.teardown_status == "succeeded"
    assert "private" not in current.error and "private" not in current.dynamic_report_text
    assert harness.container.repository.get_route_check(build.route_check_id).dynamic_report_text == current.dynamic_report_text
    assert cleanup == [build.build_id]


def test_a_pending_dynamic_result_cannot_be_mistaken_for_delivery_success(monkeypatch):
    harness, build, _, _ = pipeline()
    service, probe, _, _ = install_dynamic(harness, [])
    monkeypatch.setattr(service, "execute", lambda build_id, **kwargs: harness.container.repository.get_build(build_id))
    harness.runtime.start_build(build.build_id, "start")
    assert harness.runtime.poll_build(build.build_id, 0, "poll")["status"] == "deploying"
    assert harness.container.repository.get_build(build.build_id).dynamic_check_passed is None
    assert not probe.calls
    assert not any(event.kind == "build.deployed" for event in harness.container.repository.list_events(build.appid))


def test_published_port_failure_keeps_deployment_inventory_and_reclaims_it(monkeypatch):
    harness, build, _, _ = pipeline()
    _, probe, _, _ = install_dynamic(harness, [])
    cleanup = local_cleanup(harness, monkeypatch)

    def unavailable(deployment):
        assert harness.container.repository.get_build(build.build_id).service_ids
        raise DockerServiceError("Published port api:18080 is not reachable")

    monkeypatch.setattr(harness.runtime, "_probe_public_ports", unavailable)
    harness.runtime.start_build(build.build_id, "start")
    assert harness.runtime.poll_build(build.build_id, 0, "poll")["status"] == "failed"
    current = harness.container.repository.get_build(build.build_id)
    assert current.dynamic_check_passed is False and current.teardown_status == "succeeded"
    assert "Published port" in current.dynamic_report_text
    check = harness.container.repository.get_route_check(build.route_check_id)
    assert check.dynamic_check_passed is False and check.dynamic_report_text == current.dynamic_report_text
    assert harness.container.repository.list_services(build.appid)[0].build_id == build.build_id
    assert cleanup == [build.build_id] and not probe.calls


def test_lost_dynamic_lease_cannot_save_success_or_cleanup_another_workers_resources(monkeypatch):
    harness, build, _, _ = pipeline()

    def lose_lease():
        harness.container.repository.update_build_resources(build.build_id, {
            "lease_owner": "replacement-worker", "lease_expires_at": utc_now() + timedelta(seconds=60),
        })

    service, probe, _, _ = install_dynamic(harness, [{"passed": True, "status_code": 200}], on_probe=lose_lease)
    teardown = []
    monkeypatch.setattr(service, "teardown", lambda *a, **kw: teardown.append(a))
    harness.runtime.start_build(build.build_id, "start")
    assert harness.runtime.poll_build(build.build_id, 0, "poll")["status"] == "deploying"
    current = harness.container.repository.get_build(build.build_id)
    assert current.dynamic_check_passed is None and current.dynamic_status == "running"
    assert harness.container.repository.get_build_resources(build.build_id).lease_owner == "replacement-worker"
    assert len(probe.calls) == 1 and not teardown


def test_operation_exception_releases_lease_before_finally_cleanup(monkeypatch):
    harness, build, _, _ = pipeline()
    install_dynamic(harness, [])
    cleanup = local_cleanup(harness, monkeypatch)

    def interrupted():
        harness.container.builds.fail(build.build_id, "route failed")
        harness.container.repository.update_build(build.build_id, {
            "dynamic_check_passed": False, "dynamic_status": "failed", "teardown_required": True,
        })
        raise RuntimeError("interrupted after durable failure")

    with pytest.raises(RuntimeError, match="interrupted after durable failure"):
        harness.runtime._run_build_operation(build.build_id, interrupted)
    assert cleanup == [build.build_id]
    assert harness.container.repository.get_build_resources(build.build_id).lease_owner is None


def test_terminal_task_does_not_cleanup_while_another_worker_still_has_a_live_lease(monkeypatch):
    harness, build, _, _ = pipeline()
    harness.container.builds.fail(build.build_id, "route failed")
    harness.container.repository.update_build(build.build_id, {"teardown_required": True})
    harness.container.repository.update_build_resources(build.build_id, {
        "lease_owner": "other-worker", "lease_expires_at": utc_now() + timedelta(seconds=60),
    })
    called = []
    monkeypatch.setattr(harness.dynamic_checks, "teardown", lambda *a, **kw: called.append(a))
    assert harness.runtime.start_build(build.build_id, "delayed")["status"] == "failed"
    assert not called
    assert harness.container.repository.get_build(build.build_id).teardown_required


@pytest.mark.parametrize("record_ids", [True, False])
def test_restart_resumes_existing_deployment_without_jenkins_artifact_or_redeployment(record_ids):
    harness, build, jenkins, docker = pipeline()
    current = record_existing_deployment(harness, build)
    assert current.jenkins_build_number is None
    if not record_ids:
        harness.container.repository.update_build(build.build_id, {"service_ids": []})
    _, probe, _, _ = install_dynamic(harness, [{"passed": True, "status_code": 200}])
    assert harness.runtime.recover_builds("beat") == {"start_scheduled": 0, "poll_scheduled": 1}
    assert harness.runtime.poll_build(build.build_id, 0, "recovered")["status"] == "succeeded"
    assert len(probe.calls) == 1
    assert not jenkins.artifact_calls and not jenkins.requests and not docker.calls
    assert harness.container.repository.get_build(build.build_id).service_ids == ["service-demo-api"]


def test_resume_finishes_an_already_passed_dynamic_result_without_repeating_probe():
    harness, build, jenkins, docker = pipeline()
    record_existing_deployment(harness, build)
    harness.container.repository.update_build(build.build_id, {
        "dynamic_status": "succeeded", "dynamic_check_passed": True,
    })
    _, probe, _, _ = install_dynamic(harness, [])
    assert harness.runtime.poll_build(build.build_id, 999999, "late-poll")["status"] == "succeeded"
    assert not probe.calls and not docker.calls and not jenkins.artifact_calls


def test_interrupted_running_probe_is_not_repeated_or_redeployed_and_fails_safely(monkeypatch):
    harness, build, jenkins, docker = pipeline()
    record_existing_deployment(harness, build)
    harness.container.repository.update_build(build.build_id, {
        "dynamic_status": "running", "dynamic_run_id": "interrupted", "dynamic_initial_started": True,
    })
    install_dynamic(harness, [])
    # No Jenkins job exists in this pre-populated recovery fixture.
    cleanup = local_cleanup(harness, monkeypatch)
    assert harness.runtime.poll_build(build.build_id, 0, "recovered")["status"] == "failed"
    assert cleanup == [build.build_id]
    current = harness.container.repository.get_build(build.build_id)
    assert current.dynamic_check_passed is False
    assert not jenkins.artifact_calls and not docker.calls


def test_manual_dynamic_run_rejects_old_messages_and_completes_new_run():
    harness, build, _, _ = pipeline()
    record_existing_deployment(harness, build)
    service, probe, _, _ = install_dynamic(harness, [
        {"passed": True, "status_code": 200}, {"passed": True, "status_code": 200},
    ])
    assert harness.runtime.dynamic_route_check(build.build_id)["status"] == "succeeded"
    first_run = harness.container.repository.get_build(build.build_id).dynamic_run_id
    submitted = service.submit(build.appid, build.build_id)
    assert submitted.status is BuildStatus.DEPLOYING and submitted.dynamic_status == "queued"
    assert submitted.dynamic_run_id != first_run
    assert harness.runtime.dynamic_route_check(build.build_id, first_run)["status"] == "deploying"
    assert len(probe.calls) == 1
    assert harness.runtime.dynamic_route_check(build.build_id, submitted.dynamic_run_id)["status"] == "succeeded"
    assert len(probe.calls) == 2
    assert harness.container.dispatcher.dynamic_check_runs[-1] == (build.build_id, submitted.dynamic_run_id)


def test_route_check_recovery_dispatches_queued_manual_dynamic_runs():
    harness, build, _, _ = pipeline()
    record_existing_deployment(harness, build)
    service, _, _, _ = install_dynamic(harness, [])
    submitted = service.submit(build.appid, build.build_id)
    harness.container.dispatcher.dynamic_check_runs.clear()
    result = harness.runtime.recover_route_checks()
    assert result == {"static_checks_scheduled": 0, "dynamic_checks_scheduled": 1, "teardowns_recovered": 0}
    assert harness.container.dispatcher.dynamic_check_runs == [(build.build_id, submitted.dynamic_run_id)]


@pytest.mark.parametrize("cleanup_status,teardown_status,required", [
    (CleanupStatus.SUCCEEDED, "succeeded", False),
    (CleanupStatus.PARTIAL_FAILED, "partial_failed", True),
    (CleanupStatus.RUNNING, "running", True),
])
def test_standalone_cleanup_task_synchronizes_dynamic_teardown_state(monkeypatch, cleanup_status, teardown_status, required):
    harness, build, _, _ = pipeline()
    install_dynamic(harness, [])
    harness.container.builds.fail(build.build_id, "route failed")
    harness.container.repository.update_build(build.build_id, {"teardown_required": True, "teardown_status": "pending"})

    def run(runner, build_id):
        harness.container.repository.update_build_resources(build_id, {"cleanup_status": cleanup_status})
        return {"build_id": build_id, "cleanup_status": cleanup_status.value}

    monkeypatch.setattr(BuildCleanupRunner, "run", run)
    result = harness.runtime.cleanup_build(build.build_id)
    current = harness.container.repository.get_build(build.build_id)
    assert result == {"build_id": build.build_id, "cleanup_status": cleanup_status.value}
    assert current.teardown_status == teardown_status and current.teardown_required is required


def test_runtime_attaches_the_same_injected_jenkins_to_dynamic_cleanup():
    harness, _, jenkins, _ = pipeline()
    service, _, _, _ = install_dynamic(harness, [])
    assert harness.runtime.dynamic_checks is service
    assert service.resources.jenkins_factory() is jenkins


def test_default_runtime_uses_check_services_from_its_application_container(monkeypatch):
    import orchestrator.container

    harness, _, jenkins, _ = pipeline()
    monkeypatch.setattr(orchestrator.container, "create_container", lambda settings: harness.container)
    runtime = WorkerRuntime(harness.container.settings, jenkins=jenkins)
    assert runtime.repository is harness.container.repository
    assert runtime.route_checks is harness.container.route_checks
    assert runtime.dynamic_checks is harness.container.dynamic_checks
    assert runtime.dynamic_checks.resources.jenkins_factory() is jenkins
