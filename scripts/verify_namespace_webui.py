"""Verify namespace-related WebUI build polling against a real test control plane.

This opt-in helper creates one uniquely named, successful nginx test app through
Chromium's UI. It deliberately records only identifiers, build metadata,
request counts, status transitions, and boolean checks. Credentials, form
payloads, Compose, API error bodies, and logs are never printed or written to
the result.

The operator must supply --repository-url and --git-ref for a repository that
the test environment can access; a fixed commit is recommended. The helper
does not contact that repository itself. --published-port optionally exposes
nginx's port 80 for a separate HTTP check; no host port is published by default.
While waiting, stderr reports only status and counts. A terminal API response
must appear in the UI within 10 seconds or verification fails; it never treats
the API response alone as a successful UI check.

The script does not clean up applications or build history. Verify that the
target is the disposable test environment before running it.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
from typing import Any
from urllib.parse import unquote, urlsplit

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API = "http://10.32.12.110:18080"
DEFAULT_ENV_FILE = Path("/opt/orchestrator-test/.env")
DEFAULT_SOURCE_IMAGE = "nginx:alpine"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
BUILD_STATUSES = {"queued", "validating", "triggering", "building", "deploying"} | TERMINAL_STATUSES
TERMINAL_UI_GRACE_SECONDS = 10
POLL_PROGRESS_INTERVAL_SECONDS = 15


class BuildVerificationError(RuntimeError):
    """Retain safe build evidence when verification fails, not exception text."""

    def __init__(self, reason: str, final: dict[str, Any] | None, checks: dict[str, bool]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.final = safe_build_summary(final) if final else None
        self.checks = dict(checks)


def live_delivery_helpers() -> Any:
    """Load the sibling helper by file path so direct script execution works."""

    helper_path = ROOT / "scripts" / "live_webui_delivery.py"
    spec = importlib.util.spec_from_file_location("_live_webui_delivery", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load live WebUI helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("LIVE_WEBUI_API_URL", DEFAULT_API))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--source-image", default=DEFAULT_SOURCE_IMAGE)
    parser.add_argument("--repository-url", required=True, help="Accessible source repository for the isolated test app")
    parser.add_argument("--git-ref", required=True, help="Source commit, tag, or branch; prefer a fixed commit")
    parser.add_argument("--published-port", type=int, default=None, help="Optional host port mapped to nginx port 80")
    parser.add_argument("--run", default=os.environ.get("LIVE_WEBUI_RUN", datetime.now().strftime("%H%M%S")))
    parser.add_argument("--result", type=Path, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    options = parser.parse_args(argv)
    if options.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be a positive integer")
    if not options.source_image.strip():
        parser.error("--source-image cannot be empty")
    options.repository_url = options.repository_url.strip()
    options.git_ref = options.git_ref.strip()
    if not options.repository_url:
        parser.error("--repository-url cannot be empty")
    if not options.git_ref:
        parser.error("--git-ref cannot be empty")
    if options.published_port is not None and not 1 <= options.published_port <= 65535:
        parser.error("--published-port must be between 1 and 65535")
    return options


def unique_appid(prefix: str, run: str) -> str:
    safe_run = re.sub(r"[^A-Za-z0-9-]+", "-", run).strip("-").upper() or "RUN"
    suffix = uuid.uuid4().hex[:8].upper()
    stem = f"{prefix}-{date.today():%Y%m%d}-{safe_run}"
    return f"{stem[:64 - len(suffix) - 1]}-{suffix}"


def release_compose(image: str, *, published_port: int | None = None) -> dict[str, Any]:
    web: dict[str, Any] = {"image": image, "volumes": ["data:/var/lib/release"]}
    if published_port is not None:
        web["ports"] = [f"{published_port}:80"]
    return {
        "services": {"web": web},
        "volumes": {"data": {}},
    }


def safe_build_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Whitelist build metadata; never retain error bodies, credentials or logs."""

    number = payload.get("jenkins_build_number")
    status = payload.get("status")
    return {
        "build_id": payload.get("build_id") if isinstance(payload.get("build_id"), str) else None,
        "appid": payload.get("appid") if isinstance(payload.get("appid"), str) else None,
        "jenkins_build_number": number if type(number) is int else None,
        "status": status if isinstance(status, str) and status in BUILD_STATUSES else None,
        "updated_at": payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else None,
    }


def empty_build_info_record() -> dict[str, Any]:
    return {
        "request_count": 0,
        "response_count": 0,
        "terminal_response_count": 0,
        "status_changes": [],
        "last_build": None,
        "last_terminal_build": None,
    }


def install_api_observer(page: Page) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "build_create": {},
        "build_info": {},
    }

    def build_create_record(path: str) -> dict[str, Any] | None:
        marker = "/api/v1/jenkins/build_create/"
        if marker not in path:
            return None
        appid = unquote(path.split(marker, 1)[1].rstrip("/"))
        return evidence["build_create"].setdefault(appid, {"request_count": 0, "http_statuses": []})

    def build_info_record(path: str) -> tuple[str, dict[str, Any]] | None:
        marker = "/api/v1/jenkins/build_info/"
        if marker not in path:
            return None
        build_id = unquote(path.split(marker, 1)[1].rstrip("/"))
        return build_id, evidence["build_info"].setdefault(build_id, empty_build_info_record())

    def on_request(request: Any) -> None:
        path = urlsplit(request.url).path
        if request.method == "POST":
            record = build_create_record(path)
            if record is not None:
                record["request_count"] += 1
        elif request.method == "GET":
            entry = build_info_record(path)
            if entry is not None:
                entry[1]["request_count"] += 1

    def on_response(response: Any) -> None:
        request = response.request
        path = urlsplit(response.url).path
        if request.method == "POST":
            record = build_create_record(path)
            if record is not None:
                record["http_statuses"].append(response.status)
                return
        entry = build_info_record(path) if request.method == "GET" else None
        if entry is None:
            return
        build_id, record = entry
        record["response_count"] += 1
        if not 200 <= response.status < 300:
            return
        try:
            payload = response.json()
        except Exception:
            return
        if not isinstance(payload, dict) or payload.get("build_id") != build_id:
            return
        status = payload.get("status")
        if not isinstance(status, str) or status not in BUILD_STATUSES:
            return
        summary = safe_build_summary(payload)
        record["last_build"] = summary
        if not record["status_changes"] or record["status_changes"][-1] != status:
            record["status_changes"].append(status)
        if status in TERMINAL_STATUSES:
            record["terminal_response_count"] += 1
            record["last_terminal_build"] = summary

    page.on("request", on_request)
    page.on("response", on_response)
    return evidence


def build_record_from_ui(page: Page, selector: str, build_id: str) -> dict[str, Any] | None:
    try:
        text = page.locator(selector).text_content()
    except PlaywrightError:
        raise BuildVerificationError("ui_build_output_unreadable", None, {"ui_build_output_readable": False}) from None
    try:
        payload = json.loads(text or "")
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("build_id") == build_id:
        return payload
    return None


def visible_build_status(page: Page) -> str:
    """Read DOM text, not CSS-transformed inner_text (the pill is uppercase)."""

    try:
        value = page.locator("#buildStatus").text_content()
    except PlaywrightError:
        raise BuildVerificationError("ui_build_status_unreadable", None, {"ui_build_status_readable": False}) from None
    return value.strip().lower() if value else ""


def emit_poll_progress(status: str, trace: dict[str, Any], elapsed_seconds: float) -> None:
    """Emit only safe status/count diagnostics; never identifiers or payloads."""

    safe_status = status if status in BUILD_STATUSES else "unknown"
    print(
        "build_poll "
        f"status={safe_status} "
        f"requests={trace['request_count']} "
        f"responses={trace['response_count']} "
        f"terminal_responses={trace['terminal_response_count']} "
        f"elapsed_seconds={int(elapsed_seconds)}",
        file=sys.stderr,
        flush=True,
    )


def submit_build_from_ui(page: Page, appid: str, helpers: Any, git_ref: str) -> dict[str, Any]:
    helpers.navigate_section(page, "buildSection")
    page.locator("#appId").evaluate(
        "(node, value) => { node.value = value; node.dispatchEvent(new Event('input', {bubbles:true})); }",
        arg=appid,
    )
    page.locator("#buildGitRef").fill(git_ref)
    page.locator("#buildOutput").evaluate("node => { node.textContent = ''; }")
    page.locator('#buildForm button[type="submit"]').click()
    page.wait_for_function(
        "(appid) => { const text = document.querySelector('#buildOutput')?.textContent || ''; "
        "return text.includes('build_id') && text.includes(appid); }",
        arg=appid,
        timeout=60_000,
    )
    payload = helpers.json_from_pre(page, "#buildOutput")
    if not isinstance(payload, dict) or payload.get("appid") != appid or not payload.get("build_id"):
        raise RuntimeError("WebUI build receipt did not match the submitted app")
    return payload


def wait_for_terminal_ui(
    page: Page,
    evidence: dict[str, Any],
    appid: str,
    build_id: str,
    expected_status: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, bool]]:
    started = time.monotonic()
    deadline = started + timeout_seconds
    terminal_grace_deadline: float | None = None
    final: dict[str, Any] | None = None
    trace = evidence["build_info"].setdefault(build_id, empty_build_info_record())
    last_progress_key: tuple[Any, ...] | None = None
    last_progress_at = started
    while time.monotonic() < deadline:
        now = time.monotonic()
        current = build_record_from_ui(page, "#buildOutput", build_id)
        if current and current.get("appid") == appid:
            final = safe_build_summary(current)
        observed_terminal = trace["last_terminal_build"]
        if observed_terminal and observed_terminal.get("appid") == appid and observed_terminal.get("status") != expected_status:
            raise BuildVerificationError("unexpected_terminal_status", observed_terminal, {"terminal_status_matches_expectation": False})
        if final and final.get("status") in TERMINAL_STATUSES and final.get("status") != expected_status:
            raise BuildVerificationError("unexpected_terminal_status", final, {"terminal_status_matches_expectation": False})
        visible_status = visible_build_status(page)
        if observed_terminal and observed_terminal.get("appid") == appid and terminal_grace_deadline is None:
            terminal_grace_deadline = min(deadline, now + TERMINAL_UI_GRACE_SECONDS)
        latest = trace["last_build"] or {}
        progress_key = (
            visible_status,
            latest.get("status"),
            trace["request_count"],
            trace["response_count"],
            trace["terminal_response_count"],
        )
        if progress_key != last_progress_key or now - last_progress_at >= POLL_PROGRESS_INTERVAL_SECONDS:
            emit_poll_progress(visible_status or latest.get("status", ""), trace, now - started)
            last_progress_key = progress_key
            last_progress_at = now
        if final and final.get("appid") == appid and final.get("status") == expected_status and visible_status == expected_status:
            break
        if terminal_grace_deadline is not None and now >= terminal_grace_deadline:
            checks = {
                "terminal_build_info_response_observed": bool(
                    observed_terminal
                    and observed_terminal.get("appid") == appid
                    and observed_terminal.get("status") == expected_status
                ),
                "build_output_matches_terminal_build": bool(final and final.get("status") == expected_status),
                "visible_expected_terminal_status": visible_status == expected_status,
            }
            raise BuildVerificationError(
                "terminal_ui_not_reflected",
                observed_terminal,
                checks,
            )
        page.wait_for_timeout(250)

    if not final or final.get("appid") != appid or final.get("status") != expected_status:
        last_observed = trace["last_terminal_build"] or trace["last_build"]
        if last_observed and last_observed.get("appid") == appid:
            final = last_observed
        raise BuildVerificationError("terminal_state_timeout", final, {"terminal_reached_before_timeout": False})
    if visible_build_status(page) != expected_status:
        raise BuildVerificationError("visible_terminal_state_timeout", final, {"visible_expected_terminal_status": False})

    observed_terminal = trace["last_terminal_build"]
    checks = {
        "visible_expected_terminal_status": visible_build_status(page) == expected_status,
        "build_id_matches": final.get("build_id") == build_id,
        "appid_matches": final.get("appid") == appid,
        "jenkins_build_number_present": final.get("jenkins_build_number") is not None,
        "terminal_status_matches_expectation": final.get("status") == expected_status,
        "build_info_request_observed": trace["request_count"] > 0,
        "terminal_build_info_response_observed": bool(
            trace["terminal_response_count"] > 0
            and observed_terminal
            and observed_terminal.get("appid") == appid
            and observed_terminal.get("status") == expected_status
        ),
    }
    if not all(checks.values()):
        raise BuildVerificationError("terminal_evidence_missing", final, checks)
    count_at_terminal = trace["request_count"]
    # Count requests rather than responses: even failed, aborted or still
    # pending requests after a terminal response mean polling did not stop.
    page.wait_for_timeout(6_500)
    checks["polling_stopped_after_terminal"] = trace["request_count"] == count_at_terminal
    trace["terminal_request_count"] = count_at_terminal
    if not checks["polling_stopped_after_terminal"]:
        raise BuildVerificationError("polling_continued_after_terminal", final, checks)
    return final, checks


def inspect_history_checks(
    page: Page,
    helpers: Any,
    appid: str,
    build_id: str,
    expected_status: str,
) -> dict[str, bool]:
    detail = helpers.inspect_history(page, appid, build_id)
    page.wait_for_function(
        "({buildId, status}) => { try { const value = JSON.parse(document.querySelector('#historyDetailBuild').textContent); "
        "return value.build_id === buildId && value.status === status; } catch { return false; } }",
        arg={"buildId": build_id, "status": expected_status},
        timeout=60_000,
    )
    value = build_record_from_ui(page, "#historyDetailBuild", build_id)
    return {
        "history_helper_found_build": isinstance(detail.get("build"), dict) and detail["build"].get("build_id") == build_id,
        "history_build_id_matches": bool(value and value.get("build_id") == build_id),
        "history_appid_matches": bool(value and value.get("appid") == appid),
        "history_terminal_status_matches": bool(value and value.get("status") == expected_status),
    }


def public_case_result(
    *,
    name: str,
    appid: str,
    build_id: str | None,
    final: dict[str, Any] | None,
    trace: dict[str, Any] | None,
    checks: dict[str, bool],
    error_stage: str | None = None,
    error_reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "case": name,
        "appid": appid,
        "build_id": build_id,
        "jenkins_build_number": final.get("jenkins_build_number") if final else None,
        "status": final.get("status") if final else "automation_error",
        "updated_at": final.get("updated_at") if final else None,
        "build_create_request_count": 0,
        "build_create_http_statuses": [],
        "build_info_request_count": trace.get("request_count", 0) if trace else 0,
        "build_info_response_count": trace.get("response_count", 0) if trace else 0,
        "terminal_build_info_response_count": trace.get("terminal_response_count", 0) if trace else 0,
        "status_changes": trace.get("status_changes", []) if trace else [],
        "checks": checks,
    }
    if error_stage:
        result["error_stage"] = error_stage
    if error_reason:
        result["error_reason"] = error_reason
    return result


def run_case(
    page: Page,
    helpers: Any,
    evidence: dict[str, Any],
    *,
    name: str,
    appid: str,
    image: str,
    repository_url: str,
    git_ref: str,
    run: str,
    expected_status: str,
    timeout_seconds: int,
    published_port: int | None = None,
) -> dict[str, Any]:
    stage = "create_app"
    build_id: str | None = None
    final: dict[str, Any] | None = None
    checks: dict[str, bool] = {}
    try:
        compose = release_compose(image, published_port=published_port)
        config = {
            "name": f"Namespace WebUI {name}",
            "repository_url": repository_url,
            "git_ref": git_ref,
        }
        helpers.create_app(page, name, appid, compose, config, run)
        stage = "submit_build"
        receipt = submit_build_from_ui(page, appid, helpers, git_ref)
        build_id = str(receipt["build_id"])
        create_evidence = evidence["build_create"].get(appid, {"request_count": 0, "http_statuses": []})
        create_statuses = create_evidence["http_statuses"]
        receipt_202 = create_statuses == [202]
        receipt_only = "仅表示任务已受理" in page.locator("#buildPollStatus").inner_text()
        checks["build_create_http_202"] = receipt_202
        checks["http_202_receipt_only_message"] = receipt_only
        checks["exactly_one_build_create_request"] = create_evidence["request_count"] == 1
        if not all(checks.values()):
            raise RuntimeError("Build submission did not show the expected HTTP 202 receipt")

        stage = "automatic_polling"
        final, terminal_checks = wait_for_terminal_ui(
            page, evidence, appid, build_id, expected_status, timeout_seconds
        )
        checks.update(terminal_checks)
        stage = "history_inspection"
        checks.update(inspect_history_checks(page, helpers, appid, build_id, expected_status))
        checks["exactly_one_build_create_request"] = create_evidence["request_count"] == 1

        case = public_case_result(
            name=name,
            appid=appid,
            build_id=build_id,
            final=final,
            trace=evidence["build_info"].get(build_id),
            checks=checks,
        )
        case["build_create_request_count"] = create_evidence["request_count"]
        case["build_create_http_statuses"] = list(create_statuses)
        case["passed"] = all(checks.values())
        return case
    except Exception as error:
        error_reason = None
        if isinstance(error, BuildVerificationError):
            final = error.final
            checks.update(error.checks)
            error_reason = error.reason
        case = public_case_result(
            name=name,
            appid=appid,
            build_id=build_id,
            final=final,
            trace=evidence["build_info"].get(build_id) if build_id else None,
            checks=checks,
            error_stage=stage,
            error_reason=error_reason,
        )
        create_evidence = evidence["build_create"].get(appid, {"request_count": 0, "http_statuses": []})
        case["build_create_request_count"] = create_evidence["request_count"]
        case["build_create_http_statuses"] = list(create_evidence["http_statuses"])
        case["passed"] = False
        return case


def main() -> int:
    options = parse_args()
    helpers = live_delivery_helpers()
    settings = helpers.read_dotenv(options.env_file)
    run = re.sub(r"[^A-Za-z0-9-]+", "-", options.run).strip("-").upper() or "RUN"
    cases: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        executable = "/snap/bin/chromium" if Path("/snap/bin/chromium").exists() else playwright.chromium.executable_path
        browser = playwright.chromium.launch(headless=True, executable_path=executable, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            helpers.connect(page, options.api_url, settings)
            evidence = install_api_observer(page)
            cases.append(run_case(
                page,
                helpers,
                evidence,
                name="success",
                appid=unique_appid("UIREL", run),
                image=options.source_image,
                repository_url=options.repository_url,
                git_ref=options.git_ref,
                run=run,
                expected_status="succeeded",
                timeout_seconds=options.timeout_seconds,
                published_port=options.published_port,
            ))
        except Exception:
            # Do not emit browser exception strings: they can contain request
            # details. Each case reports only a stage and safe identifiers.
            if not cases:
                cases.append({
                    "case": "session",
                    "appid": None,
                    "build_id": None,
                    "jenkins_build_number": None,
                    "status": "automation_error",
                    "updated_at": None,
                    "build_create_request_count": 0,
                    "build_info_request_count": 0,
                    "status_changes": [],
                    "checks": {"connected": False},
                    "error_stage": "connect",
                    "passed": False,
                })
        finally:
            browser.close()

    output = {"run": run, "cases": cases, "passed": len(cases) == 1 and all(case.get("passed") for case in cases)}
    result_path = options.result or Path(f"/tmp/{run}-namespace-webui-results.json")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
