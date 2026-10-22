"""Persisted, build-scoped route probes and single-retry diagnostics.

``execute`` runs under the deployment worker's renewable build lease. Cleanup
deliberately happens through ``teardown`` only *after* that lease is released.
Reservations are saved before external calls: a crashed worker must never
repeat an uncertain Debug Agent call or consume a second retry.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import timezone
import json
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import uuid4

from orchestrator.agent_executor import AgentExecutionError
from orchestrator.build_resources import BuildCleanupRunner, BuildResourceService, ResourceConflict, build_lease
from orchestrator.models import AgentRole, BuildJob, BuildStatus, CleanupStatus, ResourceState, utc_now
from orchestrator.route_checks import (
    DYNAMIC_ROLE, RouteCheckConflict, RouteCheckInputError, RouteCheckUnavailableError,
)
from orchestrator.route_logs import BuildLogCollector
from orchestrator.route_probes import ProbeClient, ProbeEndpoint, validate_probe_payload
from orchestrator.services import NotFoundError


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


class DynamicCheckService:
    def __init__(self, repository: Any, dispatcher: Any, settings: Any, route_checks: Any,
                 builds: Any, resources: BuildResourceService, *, executor: Any | None = None,
                 probe_client: Any | None = None, log_collector: Any | None = None) -> None:
        self.repository = repository
        self.dispatcher = dispatcher
        self.settings = settings
        self.route_checks = route_checks
        self.builds = builds
        self.resources = resources
        self.executor = executor or route_checks.executor
        self.probe_client = probe_client or ProbeClient(
            timeout_seconds=settings.route_probe_timeout_seconds,
            max_response_bytes=settings.route_probe_max_response_bytes,
        )
        self.log_collector = log_collector or BuildLogCollector(settings, repository)

    def _build(self, build_id: str) -> BuildJob:
        build = self.repository.get_build(build_id)
        if build is None:
            raise NotFoundError("Build not found")
        return build

    def _save(self, build_id: str, fields: dict[str, Any], guard: Callable[[], None]) -> BuildJob:
        guard()
        saved = self.repository.update_build(build_id, {**fields, "updated_at": utc_now()})
        if saved is None:
            raise NotFoundError("Build not found")
        return saved

    def submit(self, app_id: str, build_id: str) -> BuildJob:
        app = self.repository.get_app(app_id)
        build = self._build(build_id)
        if app is None or build.appid != app_id:
            raise NotFoundError("Build not found for this application")
        if build.status not in {BuildStatus.DEPLOYING, BuildStatus.SUCCEEDED}:
            raise RouteCheckConflict("Dynamic checks require a running deployment")
        manifest = self.repository.get_build_resources(build_id)
        if manifest is None or manifest.cleanup_status != CleanupStatus.NOT_STARTED or build.teardown_required:
            raise RouteCheckConflict("Build resources are unavailable or being cleaned up")
        if not build.service_ids:
            raise RouteCheckConflict("Build deployment has not started its services")
        self.route_checks.verify_build_gate(build, app)
        with build_lease(self.repository, build_id, [CleanupStatus.NOT_STARTED]) as tracker:
            if tracker is None:
                raise RouteCheckConflict("A build operation is already running")
            build = self._build(build_id)
            if build.dynamic_status in {"queued", "running"}:
                return build
            if build.status not in {BuildStatus.DEPLOYING, BuildStatus.SUCCEEDED} or build.teardown_required:
                raise RouteCheckConflict("Build is no longer available for a dynamic check")
            role = self.route_checks.roles.get(DYNAMIC_ROLE)
            fields = {
                "dynamic_run_id": uuid4().hex, "dynamic_status": "queued", "dynamic_check_passed": None,
                "dynamic_initial_started": False, "dynamic_debug_started": False, "dynamic_retry_started": False,
                "dynamic_retry_payload": None, "dynamic_dispatch_error": None, "dynamic_report_text": None,
                "probe_result": None, "dynamic_agent_role_version": role.agent_role_version,
                "route_dynamic_role_snapshot": role.model_dump(mode="json"), "error": None, "finished_at": None,
            }
            tracker.guard()
            # An explicit recheck invalidates the old successful delivery until
            # this new run passes. Ordinary build transitions remain terminal.
            changed = self.repository.transition_build(build_id, [build.status], BuildStatus.DEPLOYING,
                                                        {**fields, "updated_at": utc_now()})
            if changed is None:
                raise RouteCheckConflict("Build changed concurrently")
            build = changed
            self._claim_result(build, tracker.guard, allow_claim=True)
        try:
            self.dispatcher.dispatch_dynamic_check(build_id, build.dynamic_run_id)
        except Exception:
            self.repository.update_build(build_id, {"dynamic_dispatch_error": "Unable to enqueue dynamic check"})
            raise RouteCheckUnavailableError("Unable to enqueue dynamic check; the saved run will be recovered") from None
        return self._build(build_id)

    def _claim_result(self, build: BuildJob, guard: Callable[[], None], *, allow_claim: bool = False) -> None:
        check = self.repository.get_route_check(build.route_check_id) if build.route_check_id else None
        if (check is None or check.generation != build.route_check_generation or
                check.static_check_passed is not True or check.app_id != build.appid or
                check.commit_id != build.git_commit_sha or check.require_route != build.require_route_snapshot or
                check.static_agent_role_version != build.static_agent_role_version):
            raise RouteCheckInputError("The frozen static check is unavailable or superseded")
        guard()
        if ((check.dynamic_build_id, check.dynamic_run_id) != (build.build_id, build.dynamic_run_id)
                and not allow_claim):
            # A queued/running build may be a delayed delivery after another
            # build has already claimed this shared aggregate. Only a fresh
            # build-pipeline run (still pending) may make an initial claim.
            return
        saved = self.repository.update_route_check(check.check_id, {
            "dynamic_build_id": build.build_id, "dynamic_run_id": build.dynamic_run_id,
            "dynamic_agent_role_version": build.dynamic_agent_role_version,
            "dynamic_role_snapshot": deepcopy(build.route_dynamic_role_snapshot or {}),
            "dynamic_status": "queued" if build.dynamic_status == "queued" else "running",
            "dynamic_check_passed": None, "dynamic_report_text": None, "probe_result": None, "updated_at": utc_now(),
        }, {"generation": check.generation, "dynamic_build_id": check.dynamic_build_id,
            "dynamic_run_id": check.dynamic_run_id})
        if saved is None:
            current = self.repository.get_route_check(check.check_id)
            if (current is None or current.generation != build.route_check_generation or
                    current.static_check_passed is not True or current.app_id != build.appid or
                    current.commit_id != build.git_commit_sha or current.require_route != build.require_route_snapshot or
                    current.static_agent_role_version != build.static_agent_role_version):
                raise RouteCheckInputError("The frozen static check is unavailable or superseded")
        # The per-build result remains authoritative. A competing build may
        # claim the shared static-check aggregate after our read; losing that
        # best-effort aggregate claim must not fail this deployment or prevent
        # it from recording its own result.

    def _sync_result(self, build: BuildJob, guard: Callable[[], None]) -> None:
        if not build.route_check_id:
            return
        guard()
        # Several builds may use the same immutable static check. Each build
        # retains its own result; only the most recently claimed run updates
        # the aggregate check row, and never a forced newer static generation.
        self.repository.update_route_check(build.route_check_id, {
            "dynamic_status": build.dynamic_status, "dynamic_check_passed": build.dynamic_check_passed,
            "dynamic_report_text": build.dynamic_report_text, "probe_result": deepcopy(build.probe_result),
            "dynamic_agent_role_version": build.dynamic_agent_role_version, "updated_at": utc_now(),
        }, {"generation": build.route_check_generation, "dynamic_build_id": build.build_id,
            "dynamic_run_id": build.dynamic_run_id})

    def _endpoints(self, build: BuildJob, payload: Mapping[str, Any]) -> list[ProbeEndpoint]:
        manifest = self.repository.get_build_resources(build.build_id)
        if manifest is None or manifest.appid != build.appid:
            return []
        owned = {resource.native_id: resource for resource in manifest.resources.values()
                 if resource.kind == "service" and resource.managed and resource.native_id
                 and resource.state == ResourceState.PRESENT}
        endpoints = []
        public_host = self.settings.public_host
        if public_host and public_host.startswith("[") and public_host.endswith("]"):
            public_host = public_host[1:-1]
        grpc = payload.get("protocol", "http") == "grpc"
        scheme = ("grpcs" if self.settings.public_scheme == "https" else "grpc") if grpc else self.settings.public_scheme
        for service in self.repository.list_services(build.appid):
            resource = owned.get(service.service_id)
            if service.build_id != build.build_id or service.service_id not in build.service_ids or resource is None:
                continue
            name = resource.metadata.get("compose_service") or service.service_name
            for port in service.published_ports:
                if port.protocol != "tcp":
                    continue
                if public_host and port.published_port:
                    host, number = public_host, port.published_port
                else:
                    host, number = service.service_name, port.target_port
                endpoints.append(ProbeEndpoint(service_name=name, host=host, port=number,
                                               scheme=scheme, target_port=port.target_port))
            # Internal-only services may be probed through an explicitly
            # declared Compose expose port on the worker's overlay network.
            source = (build.compose_snapshot or {}).get("services", {}).get(name, {})
            declared = {port.target_port for port in service.published_ports}
            for exposed in source.get("expose", []) if isinstance(source, Mapping) else []:
                value = str(exposed).split("/", 1)
                if len(value) > 1 and value[1] != "tcp":
                    continue
                if value[0].isdigit() and 1 <= int(value[0]) <= 65535 and int(value[0]) not in declared:
                    endpoints.append(ProbeEndpoint(service_name=name, host=service.service_name, port=int(value[0]),
                                                   scheme=scheme, target_port=int(value[0])))
        return endpoints

    @staticmethod
    def _payload_secrets(payload: Any) -> set[str]:
        secrets: set[str] = set()
        if isinstance(payload, Mapping):
            for key, value in payload.items():
                if isinstance(key, str) and re.search(r"authorization|cookie|token|key|secret|password|credential", key, re.I):
                    if isinstance(value, str):
                        secrets.add(value)
                        if key.lower() == "authorization" and " " in value:
                            kind, credential = value.split(" ", 1)
                            secrets.add(credential)
                            if kind.lower() == "basic":
                                try:
                                    decoded = base64.b64decode(credential, validate=True).decode("utf-8")
                                    secrets.add(decoded)
                                    if ":" in decoded:
                                        secrets.add(decoded.split(":", 1)[1])
                                except (ValueError, UnicodeError, binascii.Error):
                                    pass
                    elif isinstance(value, (int, float)) and not isinstance(value, bool):
                        secrets.add(str(value))
                secrets.update(DynamicCheckService._payload_secrets(value))
        elif isinstance(payload, (list, tuple)):
            for item in payload:
                secrets.update(DynamicCheckService._payload_secrets(item))
        return {secret for secret in secrets if secret}

    def _redact(self, build: BuildJob, value: Any, *, extra_secrets: set[str] | None = None) -> Any:
        secrets = []
        try:
            manifest = self.repository.get_build_resources(build.build_id)
            encrypted = manifest.environment_ciphertext if manifest else None
            environment = self.builds.application_service.secret_box.decrypt_environment(encrypted)
            secrets = [text for text in environment.values() if len(text) >= 4]
        except Exception:
            pass
        payload = build.route_probe_payload or {}
        secrets.extend(self._payload_secrets(payload))
        secrets.extend(self._payload_secrets(build.dynamic_retry_payload or {}))
        secrets.extend(extra_secrets or set())

        def scrub_base64(item: str) -> str:
            try:
                decoded = base64.b64decode(item, validate=True)
            except (ValueError, binascii.Error):
                return item
            for secret in sorted(set(secrets), key=len, reverse=True):
                if len(secret) >= 4:
                    decoded = decoded.replace(secret.encode("utf-8"), b"[REDACTED]")
                elif decoded == secret.encode("utf-8"):
                    decoded = b"[REDACTED]"
            return base64.b64encode(decoded).decode("ascii")

        def scrub(item):
            if isinstance(item, str):
                for secret in sorted(set(secrets), key=len, reverse=True):
                    if secret:
                        item = item.replace(secret, "[REDACTED]")
                item = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", item)
                return re.sub(r"(?i)((?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[=:]\s*)[^\s,;]+",
                              r"\1[REDACTED]", item)
            if isinstance(item, Mapping):
                return {
                    str(key): scrub_base64(content) if key == "body_base64" and isinstance(content, str) else scrub(content)
                    for key, content in item.items()
                }
            if isinstance(item, (list, tuple)):
                return [scrub(content) for content in item]
            return item
        return scrub(value)

    @staticmethod
    def _retry_payload(original: dict[str, Any], retry: Any, route: str) -> dict[str, Any]:
        if not isinstance(retry, dict):
            raise RouteCheckInputError("Debug Agent retry_payload must be an object or null")
        candidate = validate_probe_payload({**deepcopy(original), **retry}, require_route=route)
        # A request may be repaired, never its destination or success criteria.
        for key in ("protocol", "path", "service_name", "target_port", "assertions",
                    "descriptor_set_base64", "reflection"):
            # Python considers True == 1, even inside nested dictionaries.
            # Canonical JSON preserves those distinctions. Pin protobuf
            # descriptors too: reinterpreting response field numbers could
            # otherwise manufacture a passing response without fixing it.
            encode = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
            if encode(candidate.get(key)) != encode(original.get(key)):
                raise RouteCheckInputError("Debug Agent cannot change the route, target or original assertions")
        return candidate

    def _finish(self, build_id: str, passed: bool, result: dict[str, Any], report: str | None,
                guard: Callable[[], None]) -> BuildJob:
        build = self._build(build_id)
        report = self._redact(build, report)[:4000] if report else None
        fields = {"dynamic_status": "succeeded" if passed else "failed", "dynamic_check_passed": passed,
                  "probe_result": self._redact(build, result), "dynamic_report_text": None if passed else report,
                  "dynamic_dispatch_error": None}
        if not passed:
            fields.update(teardown_required=True, teardown_status="pending")
        build = self._save(build_id, fields, guard)
        if not passed:
            guard()
            build = self.builds.fail(build_id, report or "动态路由检查失败")
        self._sync_result(build, guard)
        return build

    def execute(self, build_id: str, *, guard: Callable[[], None]) -> BuildJob:
        build = self._build(build_id)
        report = build.dynamic_report_text
        if build.dynamic_status in {"succeeded", "failed"}:
            if build.dynamic_status == "failed" and build.status == BuildStatus.DEPLOYING:
                guard()
                build = self.builds.fail(build_id, report or "动态路由检查失败")
            self._sync_result(build, guard)
            return build
        if build.status != BuildStatus.DEPLOYING:
            raise RouteCheckConflict("Dynamic checks must run before delivery succeeds")
        result = deepcopy(build.probe_result or {"initial": None, "retry": None, "final": None})
        try:
            if not build.dynamic_run_id:
                build = self._save(build_id, {"dynamic_run_id": uuid4().hex}, guard)
            self._claim_result(build, guard, allow_claim=build.dynamic_status == "pending")
            build = self._save(build_id, {"dynamic_status": "running", "dynamic_dispatch_error": None}, guard)
            original = validate_probe_payload(build.route_probe_payload, require_route=build.require_route_snapshot)
            endpoints = self._endpoints(build, original)
            if result.get("initial") is None:
                if build.dynamic_initial_started:
                    return self._finish(build_id, False, result, "首次探针执行被中断；为避免重复请求已终止交付", guard)
                build = self._save(build_id, {"dynamic_initial_started": True}, guard)
                initial = self.probe_client.run(original, require_route=build.require_route_snapshot, endpoints=endpoints)
                result.update(initial=self._redact(build, initial), final=self._redact(build, initial))
                build = self._save(build_id, {"probe_result": result}, guard)
                self._sync_result(build, guard)
            if result["initial"].get("passed") is True:
                return self._finish(build_id, True, result, None, guard)
            if result.get("retry") is not None:
                passed = result["retry"].get("passed") is True
                return self._finish(build_id, passed, result, build.dynamic_report_text, guard)
            if build.dynamic_retry_started:
                return self._finish(build_id, False, result, "唯一一次探针重试被中断；未重复执行重试", guard)
            retry = build.dynamic_retry_payload
            report = build.dynamic_report_text
            if retry is None:
                if build.dynamic_debug_started:
                    interrupted = "Debug Agent 执行被中断；未重复调用或额外重试"
                    return self._finish(build_id, False, result, f"{report}；{interrupted}" if report else interrupted, guard)
                logs = self._redact(build, self.log_collector.collect(build_id))
                result["container_logs"] = logs
                build = self._save(build_id, {"dynamic_debug_started": True, "probe_result": result}, guard)
                self._sync_result(build, guard)
                role = AgentRole.model_validate(build.route_dynamic_role_snapshot)
                if role.role_key != DYNAMIC_ROLE or role.agent_role_version != build.dynamic_agent_role_version:
                    raise RouteCheckInputError("Dynamic agent role snapshot does not match this build")
                root = Path(self.settings.workspace_root).resolve() / "route-debug"
                root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="debug-", dir=root) as temporary:
                    output = self.executor.run_json(role, {
                        "task": "dynamic_route_debug", "app_id": build.appid, "build_id": build_id,
                        "commit_id": build.git_commit_sha, "require_route": build.require_route_snapshot,
                        "probe_payload": self._redact(build, original), "probe_result": result,
                        "container_logs": logs, "retry_budget": 1,
                    }, cwd=Path(temporary))
                guard()
                agent_report = output.get("report_text") if isinstance(output, Mapping) else None
                retry_output = output.get("retry_payload") if isinstance(output, Mapping) else None
                if isinstance(agent_report, str):
                    report = self._redact(build, agent_report, extra_secrets=self._payload_secrets(retry_output))[:4000]
                    build = self._save(build_id, {"dynamic_report_text": report}, guard)
                if not isinstance(agent_report, str) or "retry_payload" not in output:
                    raise RouteCheckInputError("Debug Agent must return retry_payload and report_text")
                if output["retry_payload"] is None:
                    return self._finish(build_id, False, result, report or "Debug Agent 未能构造可恢复的探针请求", guard)
                retry = self._retry_payload(original, output["retry_payload"], build.require_route_snapshot)
                build = self._save(build_id, {"dynamic_retry_payload": retry, "dynamic_report_text": report}, guard)
            else:
                retry = self._retry_payload(original, retry, build.require_route_snapshot)
            build = self._save(build_id, {"dynamic_retry_started": True}, guard)
            retried = self.probe_client.run(retry, require_route=build.require_route_snapshot, endpoints=endpoints)
            result.update(retry=self._redact(build, retried), final=self._redact(build, retried))
            passed = retried.get("passed") is True
            if not passed:
                report = (report or "动态路由检查失败") + "；唯一一次探针重试仍未通过，请结合最终响应修复代码或运行配置"
            return self._finish(build_id, passed, result, report, guard)
        except ResourceConflict:
            raise
        except (RouteCheckConflict, RouteCheckInputError, AgentExecutionError, ValueError) as exc:
            safe = str(exc) if isinstance(exc, (RouteCheckInputError, AgentExecutionError)) else "动态检查输入或角色配置无效"
            if report:
                safe = f"{report}；Debug Agent 重试请求未能安全执行：{safe}"
            return self._finish(build_id, False, result, safe, guard)
        except Exception:
            safe = "动态检查或 Debug Agent 执行失败；请检查受控探针和 Agent 运行环境"
            if report:
                safe = f"{report}；{safe}"
            return self._finish(build_id, False, result, safe, guard)

    def sync_teardown(self, build_id: str) -> dict[str, str]:
        build = self._build(build_id)
        manifest = self.repository.get_build_resources(build_id)
        if not build.teardown_required and build.teardown_status is None:
            return {"build_id": build_id, "teardown_status": "not_required"}
        if manifest is None:
            status = "partial_failed"
        else:
            status = {CleanupStatus.SUCCEEDED: "succeeded", CleanupStatus.PARTIAL_FAILED: "partial_failed",
                      CleanupStatus.RUNNING: "running"}.get(manifest.cleanup_status, "pending")
        self.repository.update_build(build_id, {"teardown_status": status,
            "teardown_required": status != "succeeded", "updated_at": utc_now()})
        return {"build_id": build_id, "teardown_status": status}

    def teardown(self, build_id: str, *, cleanup: Callable[[str], dict] | None = None) -> dict[str, str]:
        build = self._build(build_id)
        if not build.teardown_required:
            return self.sync_teardown(build_id)
        try:
            # dispatch=False guarantees immediate local cleanup even when the
            # broker is down; request_cleanup still enforces quiescence and
            # all normal ownership / lease / Jenkins safety checks.
            self.resources.request_cleanup(build_id, dispatch=False)
            self.repository.update_build(build_id, {"teardown_status": "running", "updated_at": utc_now()})
            if cleanup is not None:
                cleanup(build_id)
            else:
                BuildCleanupRunner(self.settings, self.resources).run(build_id)
        except Exception:
            self.repository.update_build(build_id, {"teardown_status": "partial_failed", "teardown_required": True,
                                                    "updated_at": utc_now()})
            return {"build_id": build_id, "teardown_status": "partial_failed"}
        return self.sync_teardown(build_id)

    def recover(self) -> dict[str, int]:
        counts = {"dynamic_checks_scheduled": 0, "teardowns_recovered": 0}
        for build in self.repository.list_pending_dynamic_builds():
            manifest = self.repository.get_build_resources(build.build_id)
            if manifest and manifest.lease_owner and _aware(manifest.lease_expires_at) and _aware(manifest.lease_expires_at) > utc_now():
                continue
            if build.teardown_required:
                if build.dynamic_status == "failed":
                    with build_lease(self.repository, build.build_id, [CleanupStatus.NOT_STARTED, CleanupStatus.PARTIAL_FAILED]) as tracker:
                        if tracker is None:
                            continue
                        current = self._build(build.build_id)
                        if current.dynamic_status == "failed" and current.status == BuildStatus.DEPLOYING:
                            tracker.guard()
                            current = self.builds.fail(build.build_id, current.dynamic_report_text or "动态路由检查失败")
                        if current.dynamic_status == "failed":
                            self._sync_result(current, tracker.guard)
                self.teardown(build.build_id)
                counts["teardowns_recovered"] += 1
            elif build.dynamic_status in {"queued", "running"} and build.status == BuildStatus.DEPLOYING:
                try:
                    self.dispatcher.dispatch_dynamic_check(build.build_id, build.dynamic_run_id)
                    counts["dynamic_checks_scheduled"] += 1
                except Exception:
                    continue
        return counts
