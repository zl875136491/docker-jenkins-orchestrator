#!/usr/bin/env python3
"""Read-only, real-browser acceptance for applications already on a control plane.

Run ``python3 scripts/verify_imported_apps_webui.py < private-input.json``.
Input is one JSON object with base_url (origin only), client_id, client_secret,
and apps: [{appid, name?, require_route?, git_ref?}]. Optional output_dir saves
the sanitized summary and a masked final application-list screenshot. Unknown
app fields are ignored, never copied into the report. No credentials belong in
argv. No request is mocked: permitted requests reach the real API; unexpected
writes and cross-origin requests are aborted as a safety guard.

At most 10 apps, 30 seconds per app and 360 seconds overall. Chromium/Playwright
are required. An optional ORCHESTRATOR_TEST_CHROMIUM_PATH selects an installed
browser. Exit 0 means passed, 1 failed, and 2 invalid input/missing dependency
or passed_with_warnings. Empty history/services are legitimate imported-data
observations, not proof of a live deployment. No traces, HARs, response bodies,
credentials, tokens, Compose, repository URLs or raw exceptions are written.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


MAX_APPS = 10
PER_APP_SECONDS = 30
TOTAL_SECONDS = 360
ACTION_MS = 5000
INPUT_LIMIT = 1024 * 1024
AUTH_PATHS = {"/oauth2/token", "/oauth2/refresh"}
READ_PATHS = {
    "/", "/healthz", "/favicon.ico", "/api/v1/jenkins/app_list",
    "/api/v1/jenkins/build_list", "/api/v1/docker/template_list",
    "/api/v1/docker/tech_stack_list", "/api/v1/docker/base_image_list",
}
APP_PATH = re.compile(r"^/api/v1/jenkins/(app_info|app_events|app_images|app_services|app_access|app_alerts)/([^/]+)$")


class VerificationError(RuntimeError):
    """Only a fixed, non-sensitive error code may cross the reporting boundary."""


class DeadlineExceeded(VerificationError):
    pass


def alarm_expired(_signum: int, _frame: Any) -> None:
    raise DeadlineExceeded("deadline_exceeded")


def require(condition: bool, code: str) -> None:
    if not condition:
        raise VerificationError(code)


def read_input() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(INPUT_LIMIT + 1)
    require(len(raw) <= INPUT_LIMIT, "input_too_large")
    try:
        supplied = json.loads(raw)
    except (ValueError, UnicodeError):
        raise VerificationError("invalid_json") from None
    require(isinstance(supplied, dict), "input_must_be_object")
    for key in ("base_url", "client_id", "client_secret"):
        require(isinstance(supplied.get(key), str) and bool(supplied[key]), f"missing_{key}")
    parsed = urlsplit(supplied["base_url"])
    require(parsed.scheme in {"http", "https"} and bool(parsed.hostname), "invalid_base_url")
    require(not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path in {"", "/"}, "base_url_must_be_origin_without_credentials")
    apps = supplied.get("apps")
    require(isinstance(apps, list) and 1 <= len(apps) <= MAX_APPS, "apps_count_must_be_1_to_10")
    selected: list[dict[str, Any]] = []
    for app in apps:
        require(isinstance(app, dict), "app_must_be_object")
        require(isinstance(app.get("appid"), str) and 1 <= len(app["appid"]) <= 64, "invalid_appid")
        item = {"appid": app["appid"]}
        for key in ("name", "git_ref", "require_route"):
            if key in app:
                require(isinstance(app[key], str) or (key == "require_route" and app[key] is None), "invalid_expected_app_field")
                item[key] = app[key]
        selected.append(item)
    require(len({app["appid"] for app in selected}) == len(selected), "duplicate_appids")
    output_dir = supplied.get("output_dir")
    require(output_dir is None or (isinstance(output_dir, str) and bool(output_dir)), "invalid_output_dir")
    return {"base_url": supplied["base_url"].rstrip("/"), "client_id": supplied["client_id"],
            "client_secret": supplied["client_secret"], "apps": selected, "output_dir": output_dir}


class Budget:
    def __init__(self) -> None:
        self.overall = time.monotonic() + TOTAL_SECONDS
        self.deadline = self.overall

    def arm(self, seconds: int | None = None) -> None:
        self.deadline = min(self.overall, time.monotonic() + seconds) if seconds else self.overall
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded("deadline_exceeded")
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, remaining)

    def timeout(self, page: Any) -> int:
        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise DeadlineExceeded("deadline_exceeded")
        timeout = min(ACTION_MS, remaining)
        page.set_default_timeout(timeout)
        page.set_default_navigation_timeout(timeout)
        return timeout


class NetworkEvidence:
    def __init__(self, origin: str, appids: set[str]) -> None:
        self.origin = origin
        self.appids = appids
        self.appid: str | None = None
        self.stage = "startup"
        self.active = True
        self.responses: Counter[str] = Counter()
        self.http_errors: list[dict[str, Any]] = []
        self.failed_requests: list[dict[str, Any]] = []
        self.blocked_requests: list[dict[str, Any]] = []
        self.page_errors: list[dict[str, Any]] = []
        self.history_scopes: list[dict[str, Any]] = []

    def identify(self, url: str) -> tuple[str, str | None]:
        path = unquote(urlsplit(url).path)
        match = APP_PATH.fullmatch(path)
        if match:
            return match[1], match[2] if match[2] in self.appids else None
        if path.startswith("/api/v1/workspace/"):
            appid = path.removeprefix("/api/v1/workspace/")
            return "workspace", appid if appid in self.appids else None
        if path.startswith("/ui/"):
            return "ui_asset", None
        known = {**{path: path.rsplit("/", 1)[-1] for path in READ_PATHS},
                 **{path: "authentication" for path in AUTH_PATHS}}
        return known.get(path, "unrecognized_endpoint"), None

    def guard(self, route: Any) -> None:
        request = route.request
        parsed = urlsplit(request.url)
        path = unquote(parsed.path)
        same_origin = f"{parsed.scheme}://{parsed.netloc}" == self.origin
        endpoint, appid = self.identify(request.url)
        safe_get = path in READ_PATHS or path.startswith("/ui/") or bool(APP_PATH.fullmatch(path)) or path.startswith("/api/v1/workspace/")
        allowed = same_origin and ((request.method in {"GET", "HEAD", "OPTIONS"} and safe_get)
                                   or (request.method == "POST" and path in AUTH_PATHS))
        if not allowed:
            self.blocked_requests.append({"endpoint": endpoint, "method": request.method,
                                          "appid": appid, "stage": self.stage})
            route.abort("blockedbyclient")
            return
        if path == "/api/v1/jenkins/build_list" and request.method == "GET":
            query = parse_qs(parsed.query)
            values = query.get("app_id", [])
            self.history_scopes.append({"appid": self.appid,
                                        "scoped_to_open_app": bool(self.appid and values == [self.appid])})
        route.continue_()

    def response(self, response: Any) -> None:
        if not self.active:
            return
        endpoint, appid = self.identify(response.url)
        self.responses[endpoint] += 1
        if response.status >= 400:
            self.http_errors.append({"endpoint": endpoint, "appid": appid, "status": response.status,
                                     "stage": self.stage, "expected_dependency_error": False,
                                     "optional_favicon_missing": endpoint == "favicon.ico" and response.status == 404})

    def failed(self, request: Any) -> None:
        if not self.active:
            return
        endpoint, appid = self.identify(request.url)
        failure = request.failure or ""
        category = next((label for marker, label in (("ERR_ABORTED", "aborted"), ("TIMED_OUT", "timeout"),
                        ("CONNECTION_REFUSED", "connection_refused"), ("NAME_NOT_RESOLVED", "dns_failure"),
                        ("BLOCKED_BY_CLIENT", "blocked_by_guard")) if marker in failure), "transport_failure")
        self.failed_requests.append({"endpoint": endpoint, "appid": appid, "category": category, "stage": self.stage})

    def page_error(self, error: Any) -> None:
        if self.active:
            self.page_errors.append({"appid": self.appid, "stage": self.stage, "type": type(error).__name__})

    def classify_runtime_failure(self, response: Any, payload: Any) -> bool:
        endpoint, appid = self.identify(response.url)
        detail = payload.get("detail", "") if isinstance(payload, dict) else ""
        text = detail.lower() if isinstance(detail, str) else ""
        expected = endpoint in {"app_services", "app_access"} and response.status in {422, 500, 502, 503} and (
            (any(word in text for word in ("docker", "swarm", "runtime", "运行资源"))
             and any(word in text for word in ("not configured", "unavailable", "not enabled", "未配置", "不可用"))))
        if expected:
            for item in reversed(self.http_errors):
                if item["endpoint"] == endpoint and item["appid"] == appid and item["status"] == response.status:
                    item["expected_dependency_error"] = True
                    break
        return expected

    def summary(self) -> dict[str, Any]:
        return {"response_counts": dict(self.responses), "http_errors": self.http_errors,
                "failed_requests": self.failed_requests, "blocked_requests": self.blocked_requests,
                "page_errors": self.page_errors, "history_scopes": self.history_scopes}


def response_matches(response: Any, path: str, appid: str | None = None) -> bool:
    parsed = urlsplit(response.url)
    return response.request.method == "GET" and unquote(parsed.path) == path and (
        appid is None or parse_qs(parsed.query).get("app_id") == [appid])


def read_json_response(response: Any) -> Any:
    require(response.status == 200, "required_read_endpoint_not_200")
    try:
        return response.json()
    except Exception:
        raise VerificationError("invalid_read_response_json") from None


def check_history(response: Any, appid: str) -> int:
    payload = read_json_response(response)
    require(isinstance(payload, dict) and isinstance(payload.get("items"), list), "invalid_history_shape")
    require(all(isinstance(item, dict) and item.get("appid") == appid for item in payload["items"]), "history_contains_other_app")
    return len(payload["items"])


def expect_section(page: Any, section: str, timeout: int, expect: Any) -> None:
    expect(page.locator(f"#{section}")).to_be_visible(timeout=timeout)
    expect(page).to_have_url(re.compile(rf"#{re.escape(section)}$"), timeout=timeout)


def open_card(page: Any, appid: str) -> Any:
    return page.locator(".app-card").filter(has=page.get_by_text(f"App ID: {appid}", exact=True)).get_by_role("button", name="打开应用", exact=True)


def card_pointer_evidence(button: Any, allowed_appids: set[str]) -> dict[str, Any]:
    """Record geometry/hit testing, never card text, credentials or repository URLs."""
    evidence = button.evaluate("""button => {
      const card = button.closest('.app-card');
      const c = card.getBoundingClientRect(); const b = button.getBoundingClientRect();
      const hit = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);
      const text = card.querySelector('div'); const textStyle = text ? getComputedStyle(text) : null;
      const repository = card.querySelector('.app-card-repository');
      const box = rect => ({x: Math.round(rect.x), y: Math.round(rect.y),
                            width: Math.round(rect.width), height: Math.round(rect.height)});
      return {card_box: box(c), button_box: box(b), viewport_width: window.innerWidth,
              computed_css: {card_display: getComputedStyle(card).display,
                             text_min_width: textStyle?.minWidth || null,
                             text_flex: textStyle?.flex || null,
                             repository_white_space: repository ? getComputedStyle(repository).whiteSpace : null},
              button_inside_card: b.x >= c.x - 1 && b.right <= c.right + 1
                                  && b.y >= c.y - 1 && b.bottom <= c.bottom + 1,
              center_receives_pointer: hit === button || button.contains(hit),
              hit_card_appid: hit?.closest('.app-card')?.querySelector('button')?.dataset.appid || null};
    }""")
    if evidence.get("hit_card_appid") not in allowed_appids:
        evidence["hit_card_appid"] = None
    return evidence


def verify_app(page: Any, expected: dict[str, Any], trace: NetworkEvidence, budget: Budget, expect: Any) -> dict[str, Any]:
    appid = expected["appid"]
    result: dict[str, Any] = {"appid": appid, "status": "failed", "checks": {}}
    checks = result["checks"]
    trace.appid = appid
    budget.arm(PER_APP_SECONDS)
    try:
        trace.stage = "open_application_pointer_check"
        button = open_card(page, appid)
        require(button.count() == 1, "app_card_selection_not_unique")
        expect(button).to_be_visible(timeout=budget.timeout(page))
        button.scroll_into_view_if_needed(timeout=budget.timeout(page))
        result["open_card_geometry"] = card_pointer_evidence(button, trace.appids)
        try:
            # Trial performs normal browser actionability checks without sending
            # a click. Never use force=True or DOM click to conceal a UI defect.
            button.click(trial=True, timeout=min(2000, budget.timeout(page)))
        except Exception:
            result["open_card_geometry"] = card_pointer_evidence(button, trace.appids)
            raise VerificationError("app_open_button_not_pointer_actionable") from None
        require(result["open_card_geometry"]["button_inside_card"], "app_open_button_overflows_card")
        checks["app_open_button_pointer_actionable"] = True
        trace.stage = "open_application"
        timeout = budget.timeout(page)
        with page.expect_response(lambda response: response_matches(response, f"/api/v1/jenkins/app_info/{appid}"), timeout=timeout) as detail_event, page.expect_response(
            lambda response: response_matches(response, "/api/v1/jenkins/build_list", appid), timeout=timeout
        ) as history_event:
            button.click()
        detail = read_json_response(detail_event.value)
        require(isinstance(detail, dict) and detail.get("appid") == appid, "wrong_app_detail_response")
        result["initial_history_item_count"] = check_history(history_event.value, appid)
        expect_section(page, "appSection", budget.timeout(page), expect)
        expect(page.locator("#getAppButton")).to_be_enabled(timeout=budget.timeout(page))
        require(page.locator("#appId").input_value() == appid and page.locator("#appId").get_attribute("readonly") is not None, "app_id_not_locked")
        for field, selector in (("name", "appName"), ("git_ref", "gitRef"), ("require_route", "requireRoute")):
            target = expected.get(field, detail.get(field)) or ""
            require(page.locator(f"#{selector}").input_value() == target, f"{field}_does_not_match_imported_app")
        links = page.locator("#appNavigation > a").evaluate_all("links => links.map(link => link.getAttribute('href'))")
        require(links == ["#appSection", "#buildSection", "#routeChecksSection", "#operationSection"], "incorrect_app_navigation_hierarchy")
        require(not page.locator("#operationNavigation").is_visible(), "operations_expanded_outside_operation_section")
        checks.update(detail_matches_imported_app=True, app_id_locked=True, four_app_navigation_items=True, operation_submenu_initially_closed=True)

        trace.stage = "build_and_route_views"
        budget.timeout(page)
        page.locator('.side-nav a[href="#buildSection"]').click()
        expect_section(page, "buildSection", budget.timeout(page), expect)
        page.locator('.side-nav a[href="#routeChecksSection"]').click()
        expect_section(page, "routeChecksSection", budget.timeout(page), expect)
        require(page.locator("#routeCheckAppId").input_value() == appid and page.locator("#routeCheckAppId").get_attribute("readonly") is not None, "route_check_app_not_locked")
        require(not page.locator("#operationNavigation").is_visible(), "operations_expanded_in_check_view")
        checks.update(build_view=True, route_check_view=True, route_check_scoped_to_app=True)

        trace.stage = "relogin_dialog_and_mobile_layout"
        budget.timeout(page)
        page.locator("#openLoginModal").click()
        expect(page.locator("#loginModal")).to_be_visible(timeout=budget.timeout(page))
        page.locator("#closeLoginModal").click()
        expect_section(page, "routeChecksSection", budget.timeout(page), expect)
        require(page.locator("#appId").input_value() == appid and page.locator("#selectedAppBanner").is_visible(), "closing_relogin_lost_app")
        page.set_viewport_size({"width": 390, "height": 844})
        require(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"), "mobile_page_overflows")
        require(page.locator("#routeCheckAppId").input_value() == appid and page.locator("#appNavigation").is_visible(), "mobile_navigation_lost_app")
        page.set_viewport_size({"width": 1440, "height": 1000})
        checks.update(relogin_close_preserves_app=True, mobile_layout=True)

        trace.stage = "scoped_history_operation"
        budget.timeout(page)
        page.locator('.side-nav a[href="#operationSection"]').click()
        expect_section(page, "operationSection", budget.timeout(page), expect)
        require(page.locator("#operationNavigation").is_visible(), "operation_submenu_did_not_open")
        page.locator('.operation-card[data-operation-section="historySection"]').click()
        expect_section(page, "historySection", budget.timeout(page), expect)
        require(appid in page.locator("#historyAppId").input_value() and page.locator("#historyAppId").get_attribute("readonly") is not None, "history_app_context_not_locked")
        with page.expect_response(lambda response: response_matches(response, "/api/v1/jenkins/build_list", appid), timeout=budget.timeout(page)) as event:
            page.locator("#historyRefreshButton").click()
        result["history_item_count"] = check_history(event.value, appid)
        expect(page.locator("#historyLoading")).to_have_text("", timeout=budget.timeout(page))
        checks["history_api_scoped_to_open_app"] = True

        trace.stage = "workspace_operation"
        budget.timeout(page)
        page.locator('.side-nav a[href="#operationSection"]').click()
        with page.expect_response(lambda response: response_matches(response, f"/api/v1/workspace/{appid}"), timeout=budget.timeout(page)) as event:
            page.locator('.operation-card[data-operation-section="workspaceSection"]').click()
        workspace = read_json_response(event.value)
        require(isinstance(workspace, dict) and workspace.get("appid") == appid and isinstance(workspace.get("files"), list), "wrong_workspace_response")
        expect_section(page, "workspaceSection", budget.timeout(page), expect)
        expect(page.locator("#workspaceAppId")).to_have_text(appid, timeout=budget.timeout(page))
        result["workspace_file_count"] = len(workspace["files"])
        checks["workspace_operation_scoped_to_app"] = True

        trace.stage = "runtime_resources_operation"
        budget.timeout(page)
        page.locator('.side-nav a[href="#operationSection"]').click()
        page.locator('.operation-card[data-operation-section="resourcesSection"]').click()
        expect_section(page, "resourcesSection", budget.timeout(page), expect)
        observations: list[dict[str, Any]] = []
        for resource, endpoint in (("services", "app_services"), ("access", "app_access")):
            with page.expect_response(lambda response, endpoint=endpoint: response_matches(response, f"/api/v1/jenkins/{endpoint}/{appid}"), timeout=budget.timeout(page)) as event:
                page.locator(f'button[data-resource="{resource}"]').click()
            response = event.value
            try:
                payload = response.json()
            except Exception:
                raise VerificationError("invalid_runtime_response_json") from None
            if response.status != 200:
                require(trace.classify_runtime_failure(response, payload), "unexpected_runtime_endpoint_failure")
                observations.append({"endpoint": endpoint, "state": "dependency_unavailable", "http_status": response.status})
                continue
            if resource == "services":
                require(isinstance(payload, list), "invalid_service_list_shape")
                observations.append({"endpoint": endpoint, "state": "empty_no_deployment" if not payload else "records_present", "service_count": len(payload)})
            else:
                require(isinstance(payload, dict) and payload.get("appid") == appid and isinstance(payload.get("services"), list), "wrong_access_app_context")
                observations.append({"endpoint": endpoint, "state": "empty_no_deployment" if not payload["services"] else "records_present",
                                     "service_count": len(payload["services"]), "access_available": payload.get("access_available") is True})
        result["runtime_observations"] = observations
        checks["runtime_operation_scoped_to_app"] = True
        result["status"] = "passed_with_warnings" if any(item["state"] == "dependency_unavailable" for item in observations) else "passed"
    except Exception as error:
        result["error"] = {"stage": trace.stage, "code": str(error) if isinstance(error, VerificationError) else "browser_check_failed", "type": type(error).__name__}
    finally:
        budget.arm()
        trace.stage = "return_to_application_list"
        try:
            budget.timeout(page)
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator("#appListNavigation").click()
            expect_section(page, "appListSection", budget.timeout(page), expect)
            require(not page.locator("#appNavigation").is_visible() and not page.locator("#selectedAppBanner").is_visible(), "app_context_banner_not_cleared")
            require(page.locator("#routeCheckAppId").input_value() == "" and page.locator("#routeCheckCommitId").input_value() == "", "route_check_context_not_cleared")
            require(page.locator("#historyTableBody tr").count() == 0 and page.locator("#workspaceFileRows tr").count() == 0
                    and page.locator("#serviceAccessTableBody tr").count() == 0 and page.locator("#compose").input_value() == "", "previous_app_data_not_cleared")
            checks["return_to_list_clears_app_context"] = True
        except Exception as error:
            result["status"] = "failed"
            result["return_error"] = {"code": str(error) if isinstance(error, VerificationError) else "return_to_list_failed", "type": type(error).__name__}
        trace.appid = None
    return result


def choose_chromium(runtime: Any) -> str:
    candidates = (os.environ.get("ORCHESTRATOR_TEST_CHROMIUM_PATH", ""), runtime.chromium.executable_path,
                  "/snap/bin/chromium", shutil.which("chromium") or "", shutil.which("chromium-browser") or "", shutil.which("google-chrome") or "")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise VerificationError("chromium_not_installed")


def run(options: dict[str, Any], report: dict[str, Any], budget: Budget) -> None:
    # Playwright's opt-in debug output can otherwise include form fill values.
    os.environ.pop("DEBUG", None)
    os.environ.pop("PWDEBUG", None)
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        raise VerificationError("playwright_not_installed") from None
    trace = NetworkEvidence(options["base_url"], {app["appid"] for app in options["apps"]})
    report["network"] = trace.summary()
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=choose_chromium(runtime), headless=True,
                                          args=["--no-sandbox", "--disable-dev-shm-usage"], timeout=15000)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, timezone_id="Asia/Shanghai", service_workers="block")
        context.route("**/*", trace.guard)
        page = context.new_page()
        page.on("response", trace.response)
        page.on("requestfailed", trace.failed)
        page.on("pageerror", trace.page_error)
        try:
            trace.stage = "login_and_app_list"
            page.goto(options["base_url"] + "/ui/", wait_until="domcontentloaded", timeout=budget.timeout(page))
            page.locator("#historyAutoRefresh").evaluate("control => { control.checked = false; control.dispatchEvent(new Event('change', {bubbles: true})); }")
            page.locator("#openLoginModal").click()
            page.locator("#workerName").fill(options["client_id"])
            page.locator("#workerSecret").fill(options["client_secret"])
            with page.expect_response(lambda response: response_matches(response, "/api/v1/jenkins/app_list"), timeout=budget.timeout(page)) as event:
                page.locator('#loginForm button[type="submit"]').click()
            apps = read_json_response(event.value)
            require(isinstance(apps, list), "invalid_application_list_shape")
            existing = {app.get("appid") for app in apps if isinstance(app, dict)}
            require(all(app["appid"] in existing for app in options["apps"]), "imported_app_missing_from_real_api")
            expect(page.locator("#sessionStatus")).to_have_text("已登录", timeout=budget.timeout(page))
            require(not page.locator("#appNavigation").is_visible(), "login_selected_app_unexpectedly")
            for app in options["apps"]:
                expect(open_card(page, app["appid"])).to_be_visible(timeout=budget.timeout(page))
            report["checks"] = {"real_ui_login": True, "imported_apps_in_api_and_ui_list": True, "new_browser_context": True,
                                "business_write_guard_enabled": True, "history_autorefresh_disabled": True}
            for app in options["apps"]:
                report["apps"].append(verify_app(page, app, trace, budget, expect))
            trace.stage = "final_evidence"
            if options["output_dir"]:
                directory = Path(options["output_dir"]).expanduser().resolve()
                directory.mkdir(parents=True, exist_ok=True)
                expect_section(page, "appListSection", budget.timeout(page), expect)
                screenshot = directory / "imported-apps-webui.png"
                page.screenshot(path=str(screenshot), full_page=True, timeout=budget.timeout(page),
                                mask=[page.locator(".app-card-repository"), page.locator("input, textarea, pre"), page.locator("#toastRegion")])
                report["screenshot"] = str(screenshot)
        finally:
            trace.active = False
            report["network"] = trace.summary()
            browser.close()


def main() -> int:
    started = time.monotonic()
    report: dict[str, Any] = {"schema_version": 1, "status": "failed", "mode": "real_api_read_only_business",
                              "started_at": datetime.now(timezone.utc).isoformat(), "apps": [],
                              "limits": {"max_apps": MAX_APPS, "per_app_seconds": PER_APP_SECONDS, "total_seconds": TOTAL_SECONDS},
                              "limitations": ["no_build_or_route_check_task_submitted", "no_live_deployment_verified",
                                              "no_gitlab_openai_jenkins_or_remote_host_operation"]}
    options = None
    exit_code = 1
    try:
        options = read_input()
        report["target_origin"] = options["base_url"]
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, alarm_expired)
        budget = Budget()
        budget.arm()
        run(options, report, budget)
        network = report.get("network", {})
        failed = (any(app["status"] == "failed" for app in report["apps"])
                  or bool(network.get("page_errors")) or bool(network.get("blocked_requests"))
                  or any(not item["scoped_to_open_app"] for item in network.get("history_scopes", []))
                  or any(not item["expected_dependency_error"] and not item["optional_favicon_missing"] for item in network.get("http_errors", []))
                  or any(item["category"] != "aborted" for item in network.get("failed_requests", [])))
        warnings = bool(network.get("failed_requests")) or bool(network.get("http_errors")) or any(app["status"] == "passed_with_warnings" for app in report["apps"])
        report["status"] = "failed" if failed else "passed_with_warnings" if warnings else "passed"
        exit_code = 1 if failed else 2 if warnings else 0
    except Exception as error:
        report["error"] = {"code": str(error) if isinstance(error, VerificationError) else "verification_unavailable_or_failed", "type": type(error).__name__}
        if isinstance(error, VerificationError) and not report["apps"]:
            exit_code = 2
    finally:
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0)
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        if options and options["output_dir"]:
            try:
                directory = Path(options["output_dir"]).expanduser().resolve()
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "imported-apps-webui.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except Exception:
                report["artifact_write_failed"] = True
                if report["status"] == "passed":
                    report["status"] = "passed_with_warnings"
                    exit_code = 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
