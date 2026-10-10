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
    expect(console.page.locator("#buildStatus")).to_have_text("queued")
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
