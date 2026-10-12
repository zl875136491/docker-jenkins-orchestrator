"""Exercise the real console in a browser with an entirely mocked API.

Playwright and a Chromium executable are optional development dependencies.
When present, these tests drive the shipped HTML/JS rather than duplicating
their payload builders. No request reaches the live orchestration backend.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
from threading import Thread
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest


playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect
ROOT = Path(__file__).resolve().parents[1]
OLD_COMMIT = "1" * 40
NEW_COMMIT = "2" * 40
OLD_COMPOSE = {"services": {"web": {"image": "example/web:old", "ports": ["18088:8080"]}}}
NEW_COMPOSE = {
    "services": {
        "web": {
            "image": "example/web:new",
            "ports": ["18092:8080"],
            "volumes": ["web-data:/workspace"],
        }
    },
    "volumes": {"web-data": {}},
}


def app_record(appid: str = "alpha", *, compose: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "appid": appid,
        "name": f"{appid} application",
        "repository_url": f"https://git.example.test/group/{appid}.git",
        "git_ref": "master",
        "environment_keys": ["EXISTING_PASSWORD"],
        "compose": deepcopy(OLD_COMPOSE if compose is None else compose),
        "compose_source_file": "docker-compose.yaml",
        "compose_source_commit": OLD_COMMIT,
        "components": [],
        "created_at": "2026-10-10T08:00:00Z",
        "updated_at": "2026-10-10T08:00:00Z",
    }


@dataclass(frozen=True)
class ApiCall:
    method: str
    path: str
    query: str
    raw_body: str | None
    body: Any


@dataclass(frozen=True)
class Reply:
    payload: Any
    status: int = 200


class MockApi:
    """Capture every API call; allow controlled failures and delayed replies."""

    def __init__(self) -> None:
        self.apps = {name: app_record(name) for name in ("alpha", "beta")}
        self.calls: list[ApiCall] = []
        self.overrides: dict[tuple[str, str], Reply | Callable[[ApiCall], Reply]] = {}
        self.paused: set[tuple[str, str]] = set()
        self.pending: list[tuple[Any, ApiCall]] = []
        self.unexpected: list[ApiCall] = []

    def matching(self, method: str, path: str) -> list[ApiCall]:
        return [call for call in self.calls if call.method == method and call.path == path]

    def handle(self, route: Any) -> None:
        request = route.request
        url = urlsplit(request.url)
        raw_body = request.post_data
        call = ApiCall(
            method=request.method,
            path=unquote(url.path),
            query=url.query,
            raw_body=raw_body,
            body=json.loads(raw_body) if raw_body else None,
        )
        self.calls.append(call)
        if (call.method, call.path) in self.paused:
            self.pending.append((route, call))
            return
        self.fulfill(route, self.reply(call))

    @staticmethod
    def fulfill(route: Any, reply: Reply) -> None:
        route.fulfill(status=reply.status, content_type="application/json", body=json.dumps(reply.payload))

    def release(self, *, reply: Reply | None = None) -> None:
        route, call = self.pending.pop(0)
        self.fulfill(route, reply or self.reply(call))

    def sync_result(self, appid: str = "alpha", *, changed: bool = True, commit_changed: bool | None = None) -> dict[str, Any]:
        app = self.apps[appid]
        commit_changed = changed if commit_changed is None else commit_changed
        result = {
            "appid": appid,
            "git_ref": app["git_ref"],
            "commit_id": NEW_COMMIT if commit_changed else app["compose_source_commit"],
            "previous_commit_id": app["compose_source_commit"],
            "compose_file": "compose.yaml" if changed else app["compose_source_file"],
            "compose_changed": changed,
            "commit_changed": commit_changed,
            "compose": deepcopy(NEW_COMPOSE if changed else app["compose"]),
            "updated_at": "2026-10-10T09:00:00Z",
        }
        app.update(
            compose=deepcopy(result["compose"]),
            compose_source_file=result["compose_file"],
            compose_source_commit=result["commit_id"],
            updated_at=result["updated_at"],
        )
        return result

    def reply(self, call: ApiCall) -> Reply:
        override = self.overrides.get((call.method, call.path))
        if override is not None:
            return override(call) if callable(override) else override
        if call.path == "/api/v1/jenkins/app_list":
            return Reply(list(self.apps.values()))
        if call.path == "/api/v1/docker/template_list":
            return Reply({"components": []})
        if call.path in ("/api/v1/docker/tech_stack_list", "/api/v1/docker/base_image_list"):
            return Reply([])
        if call.path == "/api/v1/jenkins/build_list":
            return Reply({"items": [], "page": 1, "page_size": 20, "total": 0})
        if call.path == "/api/v1/jenkins/app_create" and call.method == "POST":
            app = app_record(call.body["appid"], compose=call.body["compose"] if "compose" in call.body else NEW_COMPOSE)
            app.update({key: deepcopy(call.body[key]) for key in ("name", "repository_url", "git_ref", "components")})
            app["environment_keys"] = sorted(call.body.get("environment", {}))
            if "compose" not in call.body:
                app.update(compose_source_file="compose.yaml", compose_source_commit=NEW_COMMIT)
            else:
                app.update(compose_source_file=None, compose_source_commit=None)
            self.apps[app["appid"]] = app
            return Reply(app, 201)
        if call.path.startswith("/api/v1/jenkins/app_info/"):
            appid = call.path.rsplit("/", 1)[1]
            app = self.apps[appid]
            if call.method == "PATCH":
                for key, value in call.body.items():
                    if key == "environment":
                        app["environment_keys"] = sorted(value)
                    else:
                        app[key] = deepcopy(value)
                if any(key in call.body for key in ("compose", "repository_url", "git_ref")):
                    app.update(compose_source_file=None, compose_source_commit=None)
            return Reply(app)
        if call.path.startswith("/api/v1/jenkins/app_sync/") and call.method == "POST":
            return Reply(self.sync_result(call.path.rsplit("/", 1)[1]))
        if call.path.startswith("/api/v1/jenkins/build_create/") and call.method == "POST":
            appid = call.path.rsplit("/", 1)[1]
            return Reply({
                "build_id": "mock-submitted-build",
                "appid": appid,
                "status": "queued",
                "git_ref": call.body.get("git_ref") or self.apps[appid]["git_ref"],
                "git_commit_sha": None,
                "jenkins_build_number": None,
                "celery_task_id": "mock-celery-task",
            }, 202)
        if call.path.startswith("/api/v1/jenkins/build_info/"):
            return Reply({
                "build_id": call.path.rsplit("/", 1)[1],
                "appid": "alpha",
                "status": "failed",
                "git_ref": "master",
                "git_commit_sha": NEW_COMMIT,
                "jenkins_build_number": 43,
                "celery_task_id": "mock-celery-task",
            })
        self.unexpected.append(call)
        return Reply({"detail": "Unexpected mocked API request"}, 500)


class StaticHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if not self.path.startswith("/ui/"):
            self.send_error(404)
            return
        self.path = self.path.removeprefix("/ui")
        super().do_GET()

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with playwright.sync_playwright() as runtime:
        candidates = (
            os.environ.get("ORCHESTRATOR_TEST_CHROMIUM_PATH", ""),
            runtime.chromium.executable_path,
            "/snap/bin/chromium",
            shutil.which("chromium") or "",
            shutil.which("chromium-browser") or "",
            shutil.which("google-chrome") or "",
        )
        executable = next((candidate for candidate in candidates if candidate and Path(candidate).exists()), None)
        if executable is None:
            pytest.skip("Optional web UI browser tests need an installed Chromium executable")
        try:
            instance = runtime.chromium.launch(
                executable_path=executable,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except playwright.Error as exc:
            pytest.skip(f"Optional Chromium browser could not start: {type(exc).__name__}")
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def static_origin() -> Iterator[str]:
    handler = partial(StaticHandler, directory=str(ROOT / "webui"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@dataclass
class Console:
    page: Any
    api: MockApi

    def fill_app(self, appid: str = "new-app", *, compose: str = "") -> None:
        self.page.locator("#appId").fill(appid)
        self.page.locator("#appName").fill(f"{appid} application")
        self.page.locator("#repositoryUrl").fill(f"https://git.example.test/group/{appid}.git")
        self.page.locator("#gitRef").fill("master")
        self.page.locator("#compose").fill(compose)

    def load_app(self, appid: str = "alpha") -> dict[str, Any]:
        self.page.locator("#appId").fill(appid)
        self.page.locator("#getAppButton").click()
        expect(self.page.locator("#appName")).to_have_value(self.api.apps[appid]["name"])
        expect(self.page.locator("#composeSourceCommit")).to_have_text(self.api.apps[appid]["compose_source_commit"])
        return deepcopy(self.api.apps[appid])

    def sync(self, *, accept: bool = True) -> list[str]:
        messages: list[str] = []

        def confirm(dialog: Any) -> None:
            messages.append(dialog.message)
            dialog.accept() if accept else dialog.dismiss()

        self.page.once("dialog", confirm)
        self.page.locator("#syncAppButton").click()
        return messages


@pytest.fixture
def console(browser: Any, static_origin: str) -> Iterator[Console]:
    context = browser.new_context()
    context.add_init_script("localStorage.setItem('orchestrator.control.token', 'mock-browser-token');")
    page = context.new_page()
    page.set_default_timeout(5000)
    api = MockApi()
    page.route("**/api/v1/**", api.handle)
    page.route("**/oauth2/**", api.handle)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(static_origin + "/ui/")
        expect(page.locator("#sessionStatus")).to_have_text("已连接")
        expect(page.locator('#contextAppSelect option[value="alpha"]')).to_have_count(1)
        # The history workspace is hidden at startup; disable its background
        # polling without navigating away from the application editor.
        page.locator("#historyAutoRefresh").evaluate("control => { control.checked = false; control.dispatchEvent(new Event('change', {bubbles: true})); }")
        yield Console(page, api)
    finally:
        context.close()
    assert not errors, f"Console JavaScript raised: {errors}"
    assert not api.unexpected, f"Unexpected API calls: {[(call.method, call.path) for call in api.unexpected]}"


def editor_json(console: Console) -> dict[str, Any]:
    return json.loads(console.page.locator("#compose").input_value())


def pause_clock(page: Any) -> None:
    """Drive polling timers in virtual time, without waiting five real seconds."""

    page.clock.install(time="2026-10-12T00:00:00Z")
    page.clock.pause_at("2026-10-12T00:00:00.100Z")


def build_record(build_id: str, status: str, *, appid: str = "alpha") -> dict[str, Any]:
    return {
        "build_id": build_id,
        "appid": appid,
        "status": status,
        "git_ref": "master",
        "git_commit_sha": None if status == "queued" else NEW_COMMIT,
        "jenkins_build_number": None if status == "queued" else 45,
        "celery_task_id": "mock-celery-task",
        "error": "Jenkins build finished with FAILURE" if status == "failed" else None,
    }


def build_query(console: Console, build_id: str) -> None:
    console.page.locator('a[href="#buildSection"]').click()
    console.page.locator("#buildId").fill(build_id)
    console.page.locator('#buildQueryForm button[type="submit"]').click()


def history_query(console: Console, record: dict[str, Any]) -> None:
    console.api.overrides[("GET", "/api/v1/jenkins/build_list")] = Reply({"items": [record], "page": 1, "page_size": 20, "total": 1})
    for resource in ("events", "images", "services", "alerts"):
        console.api.overrides.setdefault(("GET", f"/api/v1/jenkins/app_{resource}/{record['appid']}"), Reply([]))
    console.page.locator('a[href="#historySection"]').click()
    console.page.locator("#historyRefreshButton").click()
    expect(console.page.locator(".history-detail-button")).to_have_count(1)
    console.page.locator(".history-detail-button").click()


def submit_build_event(console: Console) -> None:
    """Bypass disabled buttons to exercise the synchronous submission guard."""

    console.page.locator("#buildForm").dispatch_event("submit")


def test_blank_compose_is_omitted_and_discovery_populates_editor(console: Console) -> None:
    console.fill_app()
    console.page.locator('#appForm button[type="submit"]').click()
    expect(console.page.locator("#composeSourceCommit")).to_have_text(NEW_COMMIT)

    call = console.api.matching("POST", "/api/v1/jenkins/app_create")[0]
    assert "compose" not in call.body
    assert call.body["git_ref"] == "master"
    assert editor_json(console) == NEW_COMPOSE
    expect(console.page.locator("#composeSourceFile")).to_have_text("compose.yaml")
    assert not any("/build_create/" in call.path for call in console.api.calls)


@pytest.mark.parametrize("explicit", [{"services": {"web": {"image": "example/manual:pinned", "ports": ["18087:3000"]}}}, {}])
def test_explicit_compose_is_preserved_in_create_payload(console: Console, explicit: dict[str, Any]) -> None:
    console.fill_app("manual-app", compose=json.dumps(explicit))
    console.page.locator('#appForm button[type="submit"]').click()
    expect(console.page.locator("#appOutput")).to_contain_text('"appid": "manual-app"')

    call = console.api.matching("POST", "/api/v1/jenkins/app_create")[0]
    assert call.body["compose"] == explicit
    assert editor_json(console) == explicit
    assert console.api.apps["manual-app"]["compose_source_commit"] is None
    expect(console.page.locator("#composeSourceCommit")).not_to_have_text(NEW_COMMIT)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (422, "No conventional Docker Compose file was found in the repository"),
        (422, "Multiple Docker Compose files were found in the repository"),
        (422, "Docker Compose file contains invalid YAML"),
        (502, "Unable to clone GitLab repository"),
    ],
)
def test_discovery_errors_preserve_inputs(console: Console, status: int, message: str) -> None:
    console.api.overrides[("POST", "/api/v1/jenkins/app_create")] = Reply({"detail": message}, status)
    console.fill_app("failed-create")
    console.page.locator("#environment").fill('{"NON_SECRET":"keep-me"}')
    console.page.locator('#appForm button[type="submit"]').click()
    expect(console.page.locator("#appOutput")).to_contain_text(message)
    expect(console.page.locator("#appId")).to_have_value("failed-create")
    expect(console.page.locator("#repositoryUrl")).to_have_value("https://git.example.test/group/failed-create.git")
    expect(console.page.locator("#compose")).to_have_value("")
    expect(console.page.locator("#environment")).to_have_value('{"NON_SECRET":"keep-me"}')


def test_provenance_and_api_errors_are_text_not_executable_markup(console: Console) -> None:
    malicious = '<img src=x onerror="window.consoleInjected=true">'
    console.api.apps["alpha"].update(compose_source_file=malicious, compose_source_commit=malicious)
    console.load_app()
    expect(console.page.locator("#composeSourceFile")).to_have_text(malicious)
    expect(console.page.locator("#composeSourceCommit")).to_have_text(malicious)
    expect(console.page.locator("#composeSourcePanel img")).to_have_count(0)
    console.api.overrides[("POST", "/api/v1/jenkins/app_sync/alpha")] = Reply({"detail": malicious}, 422)
    console.sync()
    expect(console.page.locator("#toastRegion")).to_contain_text(malicious)
    expect(console.page.locator("#toastRegion img")).to_have_count(0)
    assert console.page.evaluate("window.consoleInjected === undefined") is True


def test_cancelled_sync_does_not_send_request_or_replace_draft(console: Console) -> None:
    console.load_app()
    draft = '{"services":{"draft":{"image":"example/draft:keep"}}}'
    console.page.locator("#compose").fill(draft)
    messages = console.sync(accept=False)

    assert messages and re.search(r"覆盖|替换", messages[0])
    assert not console.api.matching("POST", "/api/v1/jenkins/app_sync/alpha")
    expect(console.page.locator("#compose")).to_have_value(draft)
    expect(console.page.locator("#composeSourceCommit")).to_have_text(OLD_COMMIT)


def test_sync_busy_blocks_duplicate_submission_and_conflicting_actions(console: Console) -> None:
    console.load_app()
    console.api.paused.add(("POST", "/api/v1/jenkins/app_sync/alpha"))
    console.sync()
    expect(console.page.locator("#syncAppButton")).to_be_disabled()
    for selector in ("#getAppButton", "#patchAppButton", "#appId", '#appForm button[type="submit"]', '#buildForm button[type="submit"]'):
        expect(console.page.locator(selector)).to_be_disabled()
    console.page.once("dialog", lambda dialog: dialog.accept())
    console.page.locator("#syncAppButton").dispatch_event("click")
    assert len(console.api.matching("POST", "/api/v1/jenkins/app_sync/alpha")) == 1

    console.api.release()
    expect(console.page.locator("#syncAppButton")).to_be_enabled()
    assert editor_json(console) == NEW_COMPOSE


@pytest.mark.parametrize(("changed", "commit_changed"), [(True, True), (False, True), (False, False)])
def test_successful_sync_uses_saved_branch_and_refreshes_app_without_building(console: Console, changed: bool, commit_changed: bool) -> None:
    console.load_app()
    console.api.overrides[("POST", "/api/v1/jenkins/app_sync/alpha")] = lambda call: Reply(console.api.sync_result(changed=changed, commit_changed=commit_changed))
    console.page.locator("#gitRef").fill("unsaved-draft-branch")
    console.page.locator("#compose").fill('{"services":{"draft":{"image":"example/draft:discard"}}}')
    console.sync()
    expect(console.page.locator("#syncCommitId")).to_have_text(NEW_COMMIT if commit_changed else OLD_COMMIT)
    expect(console.page.locator("#syncAppButton")).to_be_enabled()

    call = console.api.matching("POST", "/api/v1/jenkins/app_sync/alpha")[0]
    assert call.raw_body is None
    assert call.query == ""
    assert len(console.api.matching("GET", "/api/v1/jenkins/app_info/alpha")) == 2
    assert editor_json(console) == (NEW_COMPOSE if changed else OLD_COMPOSE)
    expect(console.page.locator("#gitRef")).to_have_value("master")
    expect(console.page.locator("#syncGitRef")).to_have_text("master")
    expect(console.page.locator("#syncPreviousCommitId")).to_have_text(OLD_COMMIT)
    expect(console.page.locator("#composeSourceCommit")).to_have_text(NEW_COMMIT if commit_changed else OLD_COMMIT)
    expect(console.page.locator("#syncComposeFile")).to_have_text("compose.yaml" if changed else "docker-compose.yaml")
    output = json.loads(console.page.locator("#appSyncOutput").inner_text())
    assert output["compose_changed"] is changed
    assert output["commit_changed"] is commit_changed
    assert output["compose"] == console.api.apps["alpha"]["compose"]
    expect(console.page.locator("#syncComposeChanged")).to_have_text(re.compile(r"^(?:true|已变更|已更改|有变更|是)$" if changed else r"^(?:false|未变更|未更改|无变更|否)$", re.IGNORECASE))
    expect(console.page.locator("#syncCommitChanged")).to_have_text(re.compile(r"^(?:true|已变更|已更改|有变更|是)$" if commit_changed else r"^(?:false|未变更|未更改|无变更|否)$", re.IGNORECASE))
    assert not any("/build_create/" in item.path for item in console.api.calls)


def test_sync_success_with_refresh_failure_keeps_draft_and_reports_both_outcomes(console: Console) -> None:
    console.load_app()
    draft = '{"services":{"draft":{"image":"example/draft:keep"}}}'
    console.page.locator("#compose").fill(draft)

    def synchronize(call: ApiCall) -> Reply:
        result = console.api.sync_result()
        console.api.overrides[("GET", "/api/v1/jenkins/app_info/alpha")] = Reply({"detail": "Mock app-info refresh failed"}, 502)
        return Reply(result)

    console.api.overrides[("POST", "/api/v1/jenkins/app_sync/alpha")] = synchronize
    console.sync()
    expect(console.page.locator("#syncStatus")).to_contain_text("同步成功")
    expect(console.page.locator("#syncStatus")).to_contain_text("刷新失败")
    expect(console.page.locator("#syncAppButton")).to_be_enabled()
    expect(console.page.locator("#compose")).to_have_value(draft)
    expect(console.page.locator("#composeSourceCommit")).to_have_text(OLD_COMMIT)
    expect(console.page.locator("#syncCommitId")).to_have_text(NEW_COMMIT)
    assert len(console.api.matching("GET", "/api/v1/jenkins/app_info/alpha")) == 2

    # A successful backend sync with an unread new configuration is not safe
    # to build yet. The guard must apply even from the separate build panel.
    console.page.locator('a[href="#buildSection"]').click()
    console.page.locator('#buildForm button[type="submit"]').click()
    expect(console.page.locator("#buildOutput")).to_contain_text(re.compile(r"读取|刷新"))
    assert not console.api.matching("POST", "/api/v1/jenkins/build_create/alpha")

    # A successful explicit reread clears that guard, without issuing another
    # sync or automatically creating a build in the meantime.
    console.page.locator('a[href="#appSection"]').click()
    del console.api.overrides[("GET", "/api/v1/jenkins/app_info/alpha")]
    console.page.locator("#getAppButton").click()
    expect(console.page.locator("#composeSourceCommit")).to_have_text(NEW_COMMIT)
    assert editor_json(console) == NEW_COMPOSE
    assert len(console.api.matching("POST", "/api/v1/jenkins/app_sync/alpha")) == 1
    assert not console.api.matching("POST", "/api/v1/jenkins/build_create/alpha")
    console.page.locator('a[href="#buildSection"]').click()
    console.page.locator('#buildForm button[type="submit"]').click()
    expect(console.page.locator("#buildStatus")).to_have_text("queued（受理回执）")
    assert len(console.api.matching("POST", "/api/v1/jenkins/build_create/alpha")) == 1


@pytest.mark.parametrize("status", [422, 502])
def test_failed_sync_preserves_draft_and_existing_provenance(console: Console, status: int) -> None:
    console.load_app()
    draft = '{"services":{"draft":{"image":"example/draft:keep"}}}'
    console.page.locator("#compose").fill(draft)
    console.page.locator("#gitRef").fill("keep-draft-ref")
    console.api.overrides[("POST", "/api/v1/jenkins/app_sync/alpha")] = Reply({"detail": "Mock Compose discovery failed"}, status)
    console.sync()
    expect(console.page.locator("#appSyncOutput")).to_contain_text("Mock Compose discovery failed")
    expect(console.page.locator("#syncAppButton")).to_be_enabled()
    expect(console.page.locator("#compose")).to_have_value(draft)
    expect(console.page.locator("#gitRef")).to_have_value("keep-draft-ref")
    expect(console.page.locator("#composeSourceFile")).to_have_text("docker-compose.yaml")
    expect(console.page.locator("#composeSourceCommit")).to_have_text(OLD_COMMIT)
    assert len(console.api.matching("GET", "/api/v1/jenkins/app_info/alpha")) == 1


def test_late_sync_response_cannot_overwrite_a_changed_app_context(console: Console) -> None:
    console.load_app()
    console.api.paused.add(("POST", "/api/v1/jenkins/app_sync/alpha"))
    console.sync()
    expect(console.page.locator("#syncAppButton")).to_be_disabled()
    draft = '{"services":{"beta-draft":{"image":"example/beta:keep"}}}'
    # Busy controls prevent normal user changes; also guard programmatic/context
    # changes so an old request never replaces another application's editor.
    console.page.evaluate(
        """({draft}) => {
            const appId = document.getElementById('appId');
            appId.value = 'beta';
            appId.dispatchEvent(new Event('input', {bubbles: true}));
            appId.dispatchEvent(new Event('change', {bubbles: true}));
            document.getElementById('compose').value = draft;
        }""",
        {"draft": draft},
    )
    console.api.release()
    expect(console.page.locator("#syncAppButton")).to_be_enabled()
    expect(console.page.locator("#appId")).to_have_value("beta")
    expect(console.page.locator("#compose")).to_have_value(draft)
    assert len(console.api.matching("GET", "/api/v1/jenkins/app_info/alpha")) == 1
    console.load_app("beta")
    expect(console.page.locator("#appName")).to_have_value("beta application")


def test_patch_omits_unread_environment_but_includes_explicit_edits(console: Console) -> None:
    console.load_app()
    console.page.locator("#compose").fill("")
    console.page.locator("#patchAppButton").click()
    expect(console.page.locator("#patchAppButton")).to_be_enabled()
    expect(console.page.locator("#appOutput")).to_contain_text('"appid": "alpha"')
    calls = console.api.matching("PATCH", "/api/v1/jenkins/app_info/alpha")
    assert "environment" not in calls[0].body
    assert "compose" not in calls[0].body
    assert console.api.apps["alpha"]["compose"] == OLD_COMPOSE
    assert console.api.apps["alpha"]["environment_keys"] == ["EXISTING_PASSWORD"]

    console.page.locator("#environment").fill('{"NON_SECRET":"explicit-edit"}')
    console.page.locator("#patchAppButton").click()
    expect(console.page.locator("#appOutput")).to_contain_text('"NON_SECRET"')
    assert console.api.matching("PATCH", "/api/v1/jenkins/app_info/alpha")[1].body["environment"] == {"NON_SECRET": "explicit-edit"}
    console.page.locator("#environment").fill("{}")
    console.page.locator("#patchAppButton").click()
    expect(console.page.locator("#appOutput")).to_contain_text('"environment_keys": []')
    assert console.api.matching("PATCH", "/api/v1/jenkins/app_info/alpha")[2].body["environment"] == {}


def test_build_query_displays_immutable_commit(console: Console) -> None:
    console.load_app()
    console.page.locator('a[href="#buildSection"]').click()
    console.page.locator("#buildId").fill("mock-build-43")
    console.page.locator('#buildQueryForm button[type="submit"]').click()
    expect(console.page.locator("#buildCommit")).to_have_text(NEW_COMMIT)
    expect(console.page.locator("#buildStatus")).to_have_text("failed")
    assert not any("/build_create/" in item.path for item in console.api.calls)


@pytest.mark.parametrize("failure", ["api", "transport"])
def test_build_submission_is_mutually_exclusive_and_recovers_after_failure(console: Console, failure: str) -> None:
    console.load_app()
    pause_clock(console.page)
    key = ("POST", "/api/v1/jenkins/build_create/alpha")
    console.api.paused.add(key)
    console.page.locator('a[href="#buildSection"]').click()
    button = console.page.locator('#buildForm button[type="submit"]')
    button.click()
    expect(button).to_be_disabled()
    expect(console.page.locator("#buildForm")).to_have_attribute("aria-busy", "true")
    submit_build_event(console)
    submit_build_event(console)
    console.page.clock.run_for(60000)
    assert len(console.api.matching(*key)) == 1
    assert len(console.api.pending) == 1
    assert not any("/build_info/" in call.path for call in console.api.calls)

    console.api.paused.remove(key)
    if failure == "api":
        console.api.release(reply=Reply({"detail": "Submission unavailable"}, 503))
    else:
        route, _ = console.api.pending.pop(0)
        route.abort("failed")
    expect(button).to_be_enabled()
    expect(console.page.locator("#buildPollStatus")).to_contain_text("未收到受理回执")
    expect(console.page.locator("#buildForm")).not_to_have_attribute("aria-busy", "true")
    button.click()
    expect(console.page.locator("#buildStatus")).to_have_text("queued（受理回执）")
    expect(button).to_be_enabled()
    assert len(console.api.matching(*key)) == 2


def test_querying_another_build_does_not_cancel_or_unlock_pending_submission(console: Console) -> None:
    console.load_app()
    pause_clock(console.page)
    key = ("POST", "/api/v1/jenkins/build_create/alpha")
    console.api.paused.add(key)
    console.page.locator('a[href="#buildSection"]').click()
    button = console.page.locator('#buildForm button[type="submit"]')
    button.click()
    expect(button).to_be_disabled()
    build_query(console, "other-build")
    expect(console.page.locator("#buildStatus")).to_have_text("failed")
    submit_build_event(console)
    assert len(console.api.matching(*key)) == 1
    console.api.paused.remove(key)
    console.api.release()
    expect(button).to_be_enabled()
    expect(console.page.locator("#buildId")).to_have_value("other-build")
    expect(console.page.locator("#buildStatus")).to_have_text("failed")


def test_old_submission_cleanup_cannot_unlock_new_context_submission(console: Console) -> None:
    console.load_app()
    pause_clock(console.page)
    old_key = ("POST", "/api/v1/jenkins/build_create/alpha")
    new_key = ("POST", "/api/v1/jenkins/build_create/beta")
    console.api.paused.update([old_key, new_key])
    console.page.locator('a[href="#buildSection"]').click()
    button = console.page.locator('#buildForm button[type="submit"]')
    button.click()
    expect(button).to_be_disabled()
    console.page.locator('a[href="#appSection"]').click()
    console.load_app("beta")
    console.page.locator('a[href="#buildSection"]').click()
    expect(button).to_be_enabled()
    button.click()
    expect(button).to_be_disabled()
    console.page.wait_for_timeout(10)
    assert len(console.api.pending) == 2
    console.api.paused.remove(old_key)
    console.api.release()
    submit_build_event(console)
    expect(button).to_be_disabled()
    assert len(console.api.matching(*new_key)) == 1
    console.api.paused.remove(new_key)
    console.api.release()
    expect(button).to_be_enabled()
    expect(console.page.locator("#buildOutput")).to_contain_text('"appid": "beta"')
    expect(console.page.locator("#buildStatus")).to_have_text("queued（受理回执）")


@pytest.mark.parametrize("entry", ["manual", "poll", "history"])
@pytest.mark.parametrize("mismatch", ["build", "app", "null-app"])
def test_build_info_identity_mismatch_never_overwrites_selected_build(console: Console, entry: str, mismatch: str) -> None:
    console.load_app()
    pause_clock(console.page)
    build_id = "expected-build"
    key = ("GET", f"/api/v1/jenkins/build_info/{build_id}")
    wrong_record = build_record("unexpected-build" if mismatch == "build" else build_id, "failed", appid="beta" if mismatch == "app" else "alpha")
    if mismatch == "null-app":
        wrong_record["appid"] = None
    known_owner = entry == "poll" or (entry == "manual" and mismatch == "app")
    if known_owner:
        console.api.overrides[key] = Reply(build_record(build_id, "building"))
        build_query(console, build_id)
        expect(console.page.locator("#buildStatus")).to_have_text("building")
    console.api.overrides[key] = Reply(wrong_record)
    if entry == "history":
        history_query(console, build_record(build_id, "building"))
        expect(console.page.locator("#historyDetailBuild")).to_contain_text("与查询")
    elif entry == "manual":
        build_query(console, build_id)
    else:
        console.page.clock.run_for(5000)
    expect(console.page.locator("#buildPollStatus")).to_contain_text("与查询")
    expect(console.page.locator("#buildId")).to_have_value(build_id)
    expect(console.page.locator("#buildStatus")).to_have_text("building（未刷新）" if known_owner else "--")
    expect(console.page.locator("#buildOutput")).not_to_contain_text("unexpected-build")
    expect(console.page.locator("#buildOutput")).not_to_contain_text('"appid": "beta"')
    expect(console.page.locator("#buildOutput")).not_to_contain_text('"status": "failed"')
    console.api.overrides[key] = Reply(build_record(build_id, "succeeded"))
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")
    if entry == "history":
        expect(console.page.locator("#historyDetailBuild")).to_contain_text('"status": "succeeded"')


@pytest.mark.parametrize("entry", ["manual", "history"])
def test_build_info_without_appid_uses_requested_context_for_followup_polls(console: Console, entry: str) -> None:
    console.load_app()
    pause_clock(console.page)
    build_id = "legacy-build"
    key = ("GET", f"/api/v1/jenkins/build_info/{build_id}")
    record = build_record(build_id, "building")
    record.pop("appid")
    console.api.overrides[key] = Reply(record)
    if entry == "history":
        history_query(console, build_record(build_id, "building"))
    else:
        build_query(console, build_id)
    expect(console.page.locator("#buildStatus")).to_have_text("building")
    terminal = build_record(build_id, "succeeded")
    terminal.pop("appid")
    console.api.overrides[key] = Reply(terminal)
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")
    console.page.clock.run_for(60000)
    assert len(console.api.matching(*key)) == 2


def test_direct_build_query_can_inspect_other_app_and_pins_owner_for_manual_refresh(console: Console) -> None:
    console.load_app("alpha")
    pause_clock(console.page)
    build_id = "beta-direct-build"
    key = ("GET", f"/api/v1/jenkins/build_info/{build_id}")
    console.api.overrides[key] = Reply(build_record(build_id, "building", appid="beta"))
    build_query(console, build_id)
    expect(console.page.locator("#buildStatus")).to_have_text("building")
    expect(console.page.locator("#buildOutput")).to_contain_text('"appid": "beta"')
    expect(console.page.locator("#appId")).to_have_value("alpha")
    console.api.overrides[key] = Reply(build_record(build_id, "failed", appid="alpha"))
    console.page.locator("#pollBuildButton").click()
    expect(console.page.locator("#buildStatus")).to_have_text("building（未刷新）")
    expect(console.page.locator("#buildPollStatus")).to_contain_text("App ID 与查询上下文不匹配")
    expect(console.page.locator("#buildOutput")).to_contain_text('"appid": "beta"')
    console.api.overrides[key] = Reply(build_record(build_id, "succeeded", appid="beta"))
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")
    expect(console.page.locator("#appId")).to_have_value("alpha")


def test_submission_cleanup_preserves_concurrent_app_operation_button_state(console: Console) -> None:
    console.load_app()
    pause_clock(console.page)
    create_key = ("POST", "/api/v1/jenkins/build_create/alpha")
    app_key = ("GET", "/api/v1/jenkins/app_info/alpha")
    console.api.paused.update([create_key, app_key])
    console.page.locator('a[href="#buildSection"]').click()
    button = console.page.locator('#buildForm button[type="submit"]')
    button.click()
    expect(button).to_be_disabled()
    console.page.locator('a[href="#appSection"]').click()
    console.page.locator("#getAppButton").click()
    expect(console.page.locator("#getAppButton")).to_be_disabled()
    console.api.paused.remove(create_key)
    console.api.release(reply=Reply({"detail": "Submission unavailable"}, 503))
    expect(console.page.locator("#buildForm")).not_to_have_attribute("aria-busy", "true")
    expect(button).to_be_disabled()
    console.api.paused.remove(app_key)
    console.api.release()
    expect(console.page.locator("#getAppButton")).to_be_enabled()
    expect(button).to_be_enabled()


@pytest.mark.parametrize("terminal", ["failed", "succeeded", "cancelled"])
def test_submitted_build_polls_until_terminal_without_history_refresh(console: Console, terminal: str) -> None:
    console.load_app()
    pause_clock(console.page)
    build_id = "mock-submitted-build"
    key = ("GET", f"/api/v1/jenkins/build_info/{build_id}")
    replies = [Reply(build_record(build_id, "building")), Reply(build_record(build_id, terminal))]
    console.api.overrides[key] = lambda call: replies.pop(0)
    console.page.locator('a[href="#buildSection"]').click()
    console.page.locator('#buildForm button[type="submit"]').click()

    expect(console.page.locator("#buildStatus")).to_have_text("queued（受理回执）")
    expect(console.page.locator("#buildPollStatus")).to_contain_text("HTTP 202 仅表示任务已受理")
    assert not console.api.matching(*key)
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildStatus")).to_have_text("building")
    expect(console.page.locator("#buildPollStatus")).to_contain_text("最近刷新")
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildStatus")).to_have_text(terminal)
    expect(console.page.locator("#buildPollStatus")).to_contain_text("自动查询已停止")
    expect(console.page.locator("#buildCommit")).to_have_text(NEW_COMMIT)
    expect(console.page.locator("#jenkinsBuild")).to_have_text("45")
    if terminal == "failed":
        expect(console.page.locator("#buildPollStatus")).to_contain_text("Jenkins build finished with FAILURE")
    console.page.clock.run_for(60000)
    assert len(console.api.matching(*key)) == 2
    assert len(console.api.matching("POST", "/api/v1/jenkins/build_create/alpha")) == 1


def test_manual_queries_do_not_overlap_automatic_poll(console: Console) -> None:
    console.load_app()
    pause_clock(console.page)
    build_id = "active-build"
    key = ("GET", f"/api/v1/jenkins/build_info/{build_id}")
    console.api.overrides[key] = Reply(build_record(build_id, "building"))
    build_query(console, build_id)
    expect(console.page.locator("#buildStatus")).to_have_text("building")
    console.api.paused.add(key)
    console.page.clock.run_for(5000)
    console.page.wait_for_timeout(10)
    assert len(console.api.pending) == 1
    console.page.locator("#pollBuildButton").click()
    console.page.locator("#pollBuildButton").click()
    console.page.clock.run_for(15000)
    assert len(console.api.matching(*key)) == 2
    console.api.paused.remove(key)
    console.api.release(reply=Reply(build_record(build_id, "succeeded")))
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")


@pytest.mark.parametrize("change", ["app", "build"])
def test_late_build_response_cannot_overwrite_new_context(console: Console, change: str) -> None:
    console.load_app()
    pause_clock(console.page)
    old_id = "old-build"
    old_key = ("GET", f"/api/v1/jenkins/build_info/{old_id}")
    console.api.overrides[old_key] = Reply(build_record(old_id, "building"))
    build_query(console, old_id)
    expect(console.page.locator("#buildStatus")).to_have_text("building")
    console.api.paused.add(old_key)
    console.page.clock.run_for(5000)
    console.page.wait_for_timeout(10)
    assert len(console.api.pending) == 1
    if change == "app":
        console.page.locator('a[href="#appSection"]').click()
        console.load_app("beta")
    new_id = "new-build"
    new_appid = "beta" if change == "app" else "alpha"
    new_key = ("GET", f"/api/v1/jenkins/build_info/{new_id}")
    console.api.overrides[new_key] = Reply(build_record(new_id, "succeeded", appid=new_appid))
    build_query(console, new_id)
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")
    console.api.paused.remove(old_key)
    console.api.release(reply=Reply(build_record(old_id, "failed")))
    console.page.clock.run_for(60000)
    expect(console.page.locator("#buildId")).to_have_value(new_id)
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")
    expect(console.page.locator("#buildOutput")).to_contain_text(f'"build_id": "{new_id}"')
    assert len(console.api.matching(*old_key)) == 2
    assert len(console.api.matching(*new_key)) == 1


def test_late_create_receipt_is_ignored_after_app_switch(console: Console) -> None:
    console.load_app()
    pause_clock(console.page)
    key = ("POST", "/api/v1/jenkins/build_create/alpha")
    console.api.paused.add(key)
    console.page.locator('a[href="#buildSection"]').click()
    console.page.locator('#buildForm button[type="submit"]').click()
    console.page.wait_for_timeout(10)
    assert len(console.api.pending) == 1
    console.page.locator('a[href="#appSection"]').click()
    console.load_app("beta")
    console.api.paused.remove(key)
    console.api.release()
    console.page.clock.run_for(60000)
    expect(console.page.locator("#buildId")).to_have_value("")
    expect(console.page.locator("#buildOutput")).not_to_contain_text("mock-submitted-build")
    assert not any("/build_info/" in call.path for call in console.api.calls)


def test_poll_errors_are_stale_quiet_and_retry_with_backoff(console: Console) -> None:
    console.load_app()
    pause_clock(console.page)
    key = ("GET", "/api/v1/jenkins/build_info/mock-submitted-build")
    replies = [Reply({"detail": "Temporary upstream failure"}, 503)] * 2 + [Reply(build_record("mock-submitted-build", "succeeded"))]
    console.api.overrides[key] = lambda call: replies.pop(0)
    console.page.locator('a[href="#buildSection"]').click()
    console.page.locator('#buildForm button[type="submit"]').click()
    expect(console.page.locator("#buildStatus")).to_have_text("queued（受理回执）")
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildStatus")).to_have_text("queued（未刷新）")
    expect(console.page.locator("#buildPollStatus")).to_contain_text("尚未取得实时查询结果")
    expect(console.page.locator("#buildPollStatus")).to_contain_text("Temporary upstream failure")
    expect(console.page.locator("#toastRegion .toast")).to_have_count(0)
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildPollStatus")).to_contain_text("10 秒后重试")
    console.page.clock.run_for(9999)
    assert len(console.api.matching(*key)) == 2
    console.page.clock.run_for(1)
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")
    expect(console.page.locator("#toastRegion .toast")).to_have_count(0)
    assert len(console.api.matching(*key)) == 3


def test_transport_failure_keeps_last_status_and_recovers_without_toast(console: Console) -> None:
    console.load_app()
    pause_clock(console.page)
    build_id = "network-build"
    key = ("GET", f"/api/v1/jenkins/build_info/{build_id}")
    console.api.overrides[key] = Reply(build_record(build_id, "building"))
    build_query(console, build_id)
    expect(console.page.locator("#buildStatus")).to_have_text("building")
    pattern = f"**{key[1]}"
    console.page.route(pattern, lambda route: route.abort("failed"))
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildStatus")).to_have_text("building（未刷新）")
    expect(console.page.locator("#buildPollStatus")).to_contain_text("无法连接控制端")
    expect(console.page.locator("#buildPollStatus")).to_contain_text("最近刷新")
    expect(console.page.locator("#toastRegion .toast")).to_have_count(0)
    console.page.unroute(pattern)
    console.api.overrides[key] = Reply(build_record(build_id, "succeeded"))
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")


def test_history_inspection_tracks_build_separately_from_list_refresh(console: Console) -> None:
    console.load_app()
    pause_clock(console.page)
    build_id = "history-build"
    record = build_record(build_id, "building")
    console.api.overrides[("GET", "/api/v1/jenkins/build_list")] = Reply({"items": [record], "page": 1, "page_size": 20, "total": 1})
    key = ("GET", f"/api/v1/jenkins/build_info/{build_id}")
    console.api.overrides[key] = Reply(record)
    for resource in ("events", "images", "services", "alerts"):
        console.api.overrides[("GET", f"/api/v1/jenkins/app_{resource}/alpha")] = Reply([])
    console.page.locator('a[href="#historySection"]').click()
    console.page.locator("#historyRefreshButton").click()
    expect(console.page.locator(".history-detail-button")).to_have_count(1)
    console.page.locator(".history-detail-button").click()
    expect(console.page.locator("#historyDetailBuild")).to_contain_text('"status": "building"')
    console.api.overrides[key] = Reply(build_record(build_id, "failed"))
    list_count = len(console.api.matching("GET", "/api/v1/jenkins/build_list"))
    console.page.clock.run_for(5000)
    expect(console.page.locator("#historyDetailBuild")).to_contain_text('"status": "failed"')
    expect(console.page.locator("#buildStatus")).to_have_text("failed")
    console.page.clock.run_for(60000)
    assert len(console.api.matching(*key)) == 2
    assert len(console.api.matching("GET", "/api/v1/jenkins/build_list")) == list_count


@pytest.mark.parametrize("resource_reply", ["success", "failure"])
def test_slow_history_resources_do_not_delay_polls_or_overwrite_terminal_status(console: Console, resource_reply: str) -> None:
    console.load_app()
    pause_clock(console.page)
    build_id = "slow-resource-build"
    key = ("GET", f"/api/v1/jenkins/build_info/{build_id}")
    images_key = ("GET", "/api/v1/jenkins/app_images/alpha")
    console.api.overrides[key] = Reply(build_record(build_id, "building"))
    console.api.paused.add(images_key)
    history_query(console, build_record(build_id, "building"))
    expect(console.page.locator("#buildStatus")).to_have_text("building")
    expect(console.page.locator("#historyDetailBuild")).to_contain_text('"status": "building"')
    expect(console.page.locator("#historyDetailEvents")).to_have_text("[]")
    expect(console.page.locator("#historyDetailImages")).to_have_text("加载中...")
    expect(console.page.locator("#historyDetailStatus")).to_contain_text("关联资源仍在加载（1 个）")
    assert len(console.api.pending) == 1
    console.api.overrides[key] = Reply(build_record(build_id, "failed"))
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildStatus")).to_have_text("failed")
    expect(console.page.locator("#historyDetailBuild")).to_contain_text('"status": "failed"')
    latest_build = console.page.locator("#historyDetailBuild").text_content()
    latest_output = console.page.locator("#buildOutput").text_content()
    latest_poll_status = console.page.locator("#buildPollStatus").text_content()
    latest_refresh = console.page.locator("#historyDetailStatus").text_content().split("；", 1)[0]
    console.page.clock.run_for(60000)
    assert len(console.api.matching(*key)) == 2
    console.api.paused.remove(images_key)
    reply = Reply([{ "build_id": build_id, "tag": "late-image"}, {"build_id": "other-build", "tag": "not-selected"}]) if resource_reply == "success" else Reply({"detail": "Images unavailable"}, 503)
    console.api.release(reply=reply)
    if resource_reply == "success":
        expect(console.page.locator("#historyDetailImages")).to_contain_text("late-image")
        expect(console.page.locator("#historyDetailImages")).not_to_contain_text("not-selected")
    else:
        expect(console.page.locator("#historyDetailImages")).to_contain_text("Images unavailable")
        expect(console.page.locator("#historyDetailStatus")).to_contain_text("1 个接口返回失败")
    expect(console.page.locator("#historyDetailStatus")).to_contain_text(latest_refresh)
    expect(console.page.locator("#historyDetailStatus")).not_to_contain_text("仍在加载")
    expect(console.page.locator("#historyDetailBuild")).to_have_text(latest_build)
    expect(console.page.locator("#buildOutput")).to_have_text(latest_output)
    expect(console.page.locator("#buildPollStatus")).to_have_text(latest_poll_status)
    expect(console.page.locator("#buildStatus")).to_have_text("failed")
    assert len(console.api.matching(*key)) == 2


def test_late_history_resources_cannot_overwrite_new_inspection(console: Console) -> None:
    console.load_app()
    pause_clock(console.page)
    old_id = "old-history-build"
    new_id = "new-history-build"
    images_key = ("GET", "/api/v1/jenkins/app_images/alpha")
    console.api.overrides[("GET", f"/api/v1/jenkins/build_info/{old_id}")] = Reply(build_record(old_id, "building"))
    console.api.paused.add(images_key)
    history_query(console, build_record(old_id, "building"))
    expect(console.page.locator("#buildStatus")).to_have_text("building")
    expect(console.page.locator("#historyDetailImages")).to_have_text("加载中...")
    console.api.paused.remove(images_key)
    console.api.overrides[images_key] = Reply([{"build_id": new_id, "tag": "new-image"}])
    console.api.overrides[("GET", f"/api/v1/jenkins/build_info/{new_id}")] = Reply(build_record(new_id, "succeeded"))
    history_query(console, build_record(new_id, "succeeded"))
    expect(console.page.locator("#historyDetailImages")).to_contain_text("new-image")
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")
    console.api.release(reply=Reply([{"build_id": old_id, "tag": "old-image"}]))
    console.page.clock.run_for(60000)
    expect(console.page.locator("#historyDetailImages")).to_contain_text("new-image")
    expect(console.page.locator("#historyDetailImages")).not_to_contain_text("old-image")
    expect(console.page.locator("#historyDetailBuild")).to_contain_text(f'"build_id": "{new_id}"')
    expect(console.page.locator("#buildId")).to_have_value(new_id)
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")


def test_late_history_build_response_cannot_overwrite_new_inspection(console: Console) -> None:
    console.load_app()
    pause_clock(console.page)
    old_id = "delayed-history-build"
    old_key = ("GET", f"/api/v1/jenkins/build_info/{old_id}")
    console.api.paused.add(old_key)
    history_query(console, build_record(old_id, "building"))
    expect(console.page.locator("#historyDetailEvents")).to_have_text("[]")
    expect(console.page.locator("#historyDetailBuild")).to_have_text("加载中...")
    new_id = "current-history-build"
    console.api.overrides[("GET", f"/api/v1/jenkins/build_info/{new_id}")] = Reply(build_record(new_id, "succeeded"))
    history_query(console, build_record(new_id, "succeeded"))
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")
    console.api.paused.remove(old_key)
    console.api.release(reply=Reply(build_record(old_id, "failed")))
    console.page.clock.run_for(60000)
    expect(console.page.locator("#historyDetailBuild")).to_contain_text(f'"build_id": "{new_id}"')
    expect(console.page.locator("#historyDetailBuild")).not_to_contain_text(old_id)
    expect(console.page.locator("#buildId")).to_have_value(new_id)
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")
    assert len(console.api.matching(*old_key)) == 1


def test_history_build_owner_is_independent_of_active_app_and_preserved_for_polls(console: Console) -> None:
    console.load_app("alpha")
    pause_clock(console.page)
    build_id = "beta-history-build"
    key = ("GET", f"/api/v1/jenkins/build_info/{build_id}")
    record = build_record(build_id, "building", appid="beta")
    console.api.overrides[key] = Reply(record)
    history_query(console, record)
    expect(console.page.locator("#buildStatus")).to_have_text("building")
    expect(console.page.locator("#historyDetailMeta")).to_have_text("App ID：beta")
    expect(console.page.locator("#appId")).to_have_value("alpha")
    console.api.overrides[key] = Reply(build_record(build_id, "failed", appid="alpha"))
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildStatus")).to_have_text("building（未刷新）")
    expect(console.page.locator("#buildPollStatus")).to_contain_text("App ID 与查询上下文不匹配")
    expect(console.page.locator("#historyDetailBuild")).to_contain_text('"appid": "beta"')
    console.api.overrides[key] = Reply(build_record(build_id, "succeeded", appid="beta"))
    console.page.clock.run_for(5000)
    expect(console.page.locator("#buildStatus")).to_have_text("succeeded")
    expect(console.page.locator("#historyDetailBuild")).to_contain_text('"appid": "beta"')
    expect(console.page.locator("#appId")).to_have_value("alpha")


@pytest.mark.parametrize("end", ["logout", "unauthorized", "pagehide"])
def test_build_poll_stops_for_session_or_page_end(console: Console, end: str) -> None:
    console.load_app()
    pause_clock(console.page)
    build_id = "active-build"
    key = ("GET", f"/api/v1/jenkins/build_info/{build_id}")
    console.api.overrides[key] = Reply(build_record(build_id, "building"))
    build_query(console, build_id)
    expect(console.page.locator("#buildStatus")).to_have_text("building")
    if end == "logout":
        console.api.paused.add(key)
        console.page.clock.run_for(5000)
        console.page.wait_for_timeout(10)
        assert len(console.api.pending) == 1
        console.page.locator("#disconnectButton").click()
        expect(console.page.locator("#sessionStatus")).to_have_text("未连接")
        console.api.release(reply=Reply(build_record(build_id, "failed")))
    elif end == "unauthorized":
        console.api.overrides[key] = Reply({"detail": "Invalid token"}, 401)
        console.page.clock.run_for(5000)
        expect(console.page.locator("#sessionStatus")).to_have_text("未连接")
        expect(console.page.locator("#buildPollStatus")).to_contain_text("访问令牌失效")
    else:
        console.page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    count = len(console.api.matching(*key))
    console.page.clock.run_for(60000)
    assert len(console.api.matching(*key)) == count
    expect(console.page.locator("#buildStatus")).not_to_have_text("failed")


def test_access_region_is_populated_has_legacy_fallback_and_resets_with_app_context(console: Console) -> None:
    console.load_app()
    payload = {
        "appid": "alpha",
        "services": [],
        "access_available": False,
        "access_urls": [],
        "access_reason": "No published ports",
        "region": "beijing",
    }
    key = ("GET", "/api/v1/jenkins/app_access/alpha")
    console.api.overrides[key] = Reply(payload)
    console.page.locator('a[href="#resourcesSection"]').click()
    console.page.locator('button[data-resource="access"]').click()
    expect(console.page.locator("#serviceAccessRegion")).to_have_text("beijing")

    console.api.overrides[key] = Reply({name: value for name, value in payload.items() if name != "region"})
    console.page.locator('button[data-resource="access"]').click()
    expect(console.page.locator("#serviceAccessRegion")).to_have_text("--")

    console.api.overrides[key] = Reply(payload)
    console.page.locator('button[data-resource="access"]').click()
    expect(console.page.locator("#serviceAccessRegion")).to_have_text("beijing")
    console.page.locator('a[href="#appSection"]').click()
    console.page.locator("#appId").fill("beta")
    expect(console.page.locator("#serviceAccessRegion")).to_have_text("--")
    expect(console.page.locator("#composeSourceCommit")).to_have_text("--")
