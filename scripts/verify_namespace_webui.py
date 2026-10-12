"""Verify namespace-related WebUI build polling against a real test control plane.

This opt-in helper creates one uniquely named, successful nginx test app through
Chromium's UI. It deliberately records only identifiers, build metadata,
request counts, status transitions, and boolean checks. Credentials, form
payloads, Compose, API error bodies, and logs are never printed or written to
the result.

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
import time
import uuid
from typing import Any
from urllib.parse import unquote, urlsplit

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API = "http://10.32.12.110:18080"
DEFAULT_ENV_FILE = Path("/opt/orchestrator-test/.env")
DEFAULT_SOURCE_IMAGE = "nginx:alpine"


def live_delivery_helpers() -> Any:
    """Load the sibling helper by file path so direct script execution works."""

    helper_path = ROOT / "scripts" / "live_webui_delivery.py"
    spec = importlib.util.spec_from_file_location("_live_webui_delivery", helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load live WebUI helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("LIVE_WEBUI_API_URL", DEFAULT_API))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--source-image", default=DEFAULT_SOURCE_IMAGE)
    parser.add_argument("--run", default=os.environ.get("LIVE_WEBUI_RUN", datetime.now().strftime("%H%M%S")))
    parser.add_argument("--result", type=Path, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    options = parser.parse_args()
    if options.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be a positive integer")
    if not options.source_image.strip():
        parser.error("--source-image cannot be empty")
    return options


def unique_appid(prefix: str, run: str) -> str:
    safe_run = re.sub(r"[^A-Za-z0-9-]+", "-", run).strip("-").upper() or "RUN"
    suffix = uuid.uuid4().hex[:8].upper()
    stem = f"{prefix}-{date.today():%Y%m%d}-{safe_run}"
    return f"{stem[:64 - len(suffix) - 1]}-{suffix}"


def release_compose(image: str) -> dict[str, Any]:
    return {
        "services": {
            "web": {
                "image": image,
                "volumes": ["data:/var/lib/release"],
            }
        },
        "volumes": {"data": {}},
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

    def on_request(request: Any) -> None:
        if request.method == "POST":
            record = build_create_record(urlsplit(request.url).path)
            if record is not None:
                record["request_count"] += 1

    def on_response(response: Any) -> None:
        request = response.request
        path = urlsplit(response.url).path
        if request.method == "POST":
            record = build_create_record(path)
            if record is not None:
                record["http_statuses"].append(response.status)
                return
        marker = "/api/v1/jenkins/build_info/"
        if request.method != "GET" or marker not in path:
            return
        build_id = unquote(path.split(marker, 1)[1])
        record = evidence["build_info"].setdefault(build_id, {"request_count": 0, "status_changes": []})
        record["request_count"] += 1
        try:
            payload = response.json()
        except Exception:
            return
        status = payload.get("status") if isinstance(payload, dict) else None
        if isinstance(status, str) and status and (not record["status_changes"] or record["status_changes"][-1] != status):
            record["status_changes"].append(status)

    page.on("request", on_request)
    page.on("response", on_response)
    return evidence


def build_record_from_ui(page: Page, selector: str, build_id: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(page.locator(selector).inner_text().strip())
    except (ValueError, Exception):
        return None
    if isinstance(payload, dict) and payload.get("build_id") == build_id:
        return payload
    return None


def submit_build_from_ui(page: Page, appid: str, helpers: Any) -> dict[str, Any]:
    helpers.navigate_section(page, "buildSection")
    page.locator("#appId").evaluate(
        "(node, value) => { node.value = value; node.dispatchEvent(new Event('input', {bubbles:true})); }",
        arg=appid,
    )
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
    deadline = time.monotonic() + timeout_seconds
    final: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        final = build_record_from_ui(page, "#buildOutput", build_id)
        visible_status = page.locator("#buildStatus").inner_text().strip()
        if final and final.get("appid") == appid and final.get("status") == expected_status and visible_status == expected_status:
            break
        page.wait_for_timeout(250)

    if not final or final.get("appid") != appid or final.get("status") != expected_status:
        raise RuntimeError(f"Timed out waiting for expected terminal state: {expected_status}")
    if page.locator("#buildStatus").inner_text().strip() != expected_status:
        raise RuntimeError("The visible WebUI status did not reach the expected terminal state")

    trace = evidence["build_info"].get(build_id, {"request_count": 0, "status_changes": []})
    count_at_terminal = trace["request_count"]
    # Wait longer than one normal poll interval to verify that terminal states
    # stop future build_info requests without manually polling the UI.
    page.wait_for_timeout(6_500)
    polling_stopped = trace["request_count"] == count_at_terminal
    trace["terminal_request_count"] = count_at_terminal
    checks = {
        "visible_expected_terminal_status": page.locator("#buildStatus").inner_text().strip() == expected_status,
        "build_id_matches": final.get("build_id") == build_id,
        "appid_matches": final.get("appid") == appid,
        "jenkins_build_number_present": final.get("jenkins_build_number") is not None,
        "polling_stopped_after_terminal": polling_stopped,
        "terminal_status_matches_expectation": final.get("status") == expected_status,
    }
    if not all(checks.values()):
        raise RuntimeError("One or more terminal build checks failed")
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
        "status_changes": trace.get("status_changes", []) if trace else [],
        "checks": checks,
    }
    if error_stage:
        result["error_stage"] = error_stage
    return result


def run_case(
    page: Page,
    helpers: Any,
    evidence: dict[str, Any],
    *,
    name: str,
    appid: str,
    image: str,
    run: str,
    expected_status: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    stage = "create_app"
    build_id: str | None = None
    final: dict[str, Any] | None = None
    checks: dict[str, bool] = {}
    try:
        compose = release_compose(image)
        config = {
            "name": f"Namespace WebUI {name}",
            "repository_url": "https://example.invalid/release.git",
            "git_ref": "master",
        }
        helpers.create_app(page, name, appid, compose, config, run)
        stage = "submit_build"
        receipt = submit_build_from_ui(page, appid, helpers)
        build_id = str(receipt["build_id"])
        create_evidence = evidence["build_create"].get(appid, {"request_count": 0, "http_statuses": []})
        create_statuses = create_evidence["http_statuses"]
        receipt_202 = bool(create_statuses and create_statuses[-1] == 202)
        receipt_only = "仅表示任务已受理" in page.locator("#buildPollStatus").inner_text()
        checks["build_create_http_202"] = receipt_202
        checks["http_202_receipt_only_message"] = receipt_only
        if not receipt_202 or not receipt_only:
            raise RuntimeError("Build submission did not show the expected HTTP 202 receipt")

        stage = "automatic_polling"
        final, terminal_checks = wait_for_terminal_ui(
            page, evidence, appid, build_id, expected_status, timeout_seconds
        )
        checks.update(terminal_checks)
        stage = "history_inspection"
        checks.update(inspect_history_checks(page, helpers, appid, build_id, expected_status))

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
    except Exception:
        case = public_case_result(
            name=name,
            appid=appid,
            build_id=build_id,
            final=final,
            trace=evidence["build_info"].get(build_id) if build_id else None,
            checks=checks,
            error_stage=stage,
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
                run=run,
                expected_status="succeeded",
                timeout_seconds=options.timeout_seconds,
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
