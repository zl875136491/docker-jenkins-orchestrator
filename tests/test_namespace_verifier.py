"""Pure mock checks for the opt-in namespace WebUI verification helper.

No browser is launched, no repository or control API is contacted, and no
application/build history is created or removed by these tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


pytest.importorskip("playwright.sync_api")
from scripts import verify_namespace_webui as verifier


REPOSITORY = "https://git.example.test/team/release.git"
COMMIT = "1" * 40
APPID = "UIREL-ISOLATED"
BUILD_ID = "build-1"


@dataclass
class MockRequest:
    method: str
    url: str


@dataclass
class MockResponse:
    request: MockRequest
    status: int
    payload: Any
    invalid_json: bool = False

    @property
    def url(self) -> str:
        return self.request.url

    def json(self) -> Any:
        if self.invalid_json:
            raise ValueError("sensitive response body must not escape")
        return self.payload


class MockLocator:
    def __init__(self, page: MockPage, selector: str) -> None:
        self.page = page
        self.selector = selector

    def inner_text(self) -> str:
        value = self.page.texts.get(self.selector, "")
        # Match the shipped .status-pill { text-transform: uppercase; }.
        return value.upper() if self.selector == "#buildStatus" else value

    def text_content(self) -> str:
        return self.page.texts.get(self.selector, "")

    def fill(self, value: str) -> None:
        self.page.fills[self.selector] = value

    def evaluate(self, function: str, *, arg: Any = None) -> None:
        self.page.evaluations.append((self.selector, function, arg))

    def click(self) -> None:
        self.page.clicks.append(self.selector)


class MockPage:
    def __init__(self, clock: Any = None) -> None:
        self.clock = clock
        self.handlers: dict[str, Any] = {}
        self.texts: dict[str, str] = {"#buildPollStatus": "HTTP 202 仅表示任务已受理"}
        self.fills: dict[str, str] = {}
        self.evaluations: list[tuple[str, str, Any]] = []
        self.clicks: list[str] = []
        self.waits: list[int] = []
        self.function_args: list[Any] = []
        self.on_wait: Any = None

    def on(self, name: str, callback: Any) -> None:
        self.handlers[name] = callback

    def locator(self, selector: str) -> MockLocator:
        return MockLocator(self, selector)

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)
        if self.clock is not None:
            self.clock.elapsed += milliseconds / 1000
        if self.on_wait is not None:
            self.on_wait(milliseconds)

    def wait_for_function(self, function: str, *, arg: Any = None, timeout: int) -> None:
        self.function_args.append(arg)

    def show_build(self, payload: dict[str, Any]) -> None:
        self.texts["#buildOutput"] = json.dumps(payload)
        self.texts["#buildStatus"] = payload["status"]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    value = SimpleNamespace(elapsed=0.0)
    monkeypatch.setattr(verifier, "time", SimpleNamespace(monotonic=lambda: value.elapsed))
    return value


def build(status: str = "succeeded", **fields: Any) -> dict[str, Any]:
    return {
        "build_id": BUILD_ID,
        "appid": APPID,
        "jenkins_build_number": 17,
        "status": status,
        "updated_at": "2026-10-13T01:00:00Z",
        **fields,
    }


def info_request(page: MockPage, build_id: str = BUILD_ID) -> MockRequest:
    request = MockRequest("GET", f"https://control.example.test/api/v1/jenkins/build_info/{build_id}")
    page.handlers["request"](request)
    return request


def info_reply(page: MockPage, payload: Any, *, status: int = 200, invalid_json: bool = False) -> None:
    request = info_request(page)
    page.handlers["response"](MockResponse(request, status, payload, invalid_json))


def create_request(page: MockPage, appid: str = APPID, *, response: bool = True) -> None:
    request = MockRequest("POST", f"https://control.example.test/api/v1/jenkins/build_create/{appid}")
    page.handlers["request"](request)
    if response:
        page.handlers["response"](MockResponse(request, 202, {}))


def wait_for_terminal(page: MockPage, evidence: dict[str, Any], *, timeout: int = 900) -> Any:
    return verifier.wait_for_terminal_ui(page, evidence, APPID, BUILD_ID, "succeeded", timeout)


def test_observer_counts_requests_even_without_responses_and_isolates_ids() -> None:
    page = MockPage()
    evidence = verifier.install_api_observer(page)
    info_request(page)
    info_request(page, "other-build")
    create_request(page)
    create_request(page, "APP%2FOTHER", response=False)
    assert evidence["build_info"][BUILD_ID]["request_count"] == 1
    assert evidence["build_info"][BUILD_ID]["response_count"] == 0
    assert evidence["build_info"]["other-build"]["request_count"] == 1
    assert evidence["build_create"][APPID] == {"request_count": 1, "http_statuses": [202]}
    assert evidence["build_create"]["APP/OTHER"] == {"request_count": 1, "http_statuses": []}


@pytest.mark.parametrize(
    ("status", "payload", "invalid_json"),
    [
        (503, build(), False),
        (200, build(), True),
        (200, {"status": "succeeded"}, False),
        (200, build(build_id="other-build"), False),
        (200, build(status="sensitive-error-body"), False),
        (200, build(status={"secret": "must-not-escape"}), False),
        (200, [], False),
    ],
)
def test_failed_or_invalid_responses_are_not_terminal_evidence(status: int, payload: Any, invalid_json: bool) -> None:
    page = MockPage()
    evidence = verifier.install_api_observer(page)
    info_reply(page, payload, status=status, invalid_json=invalid_json)
    trace = evidence["build_info"][BUILD_ID]
    assert trace["request_count"] == trace["response_count"] == 1
    assert trace["terminal_response_count"] == 0
    assert trace["status_changes"] == []
    assert trace["last_terminal_build"] is None


def test_response_counts_are_independent_and_status_changes_are_deduplicated() -> None:
    page = MockPage()
    evidence = verifier.install_api_observer(page)
    request = info_request(page)
    for status in ("queued", "queued", "building", "succeeded"):
        page.handlers["response"](MockResponse(request, 200, build(status, error="PRIVATE_ERROR", logs="PRIVATE_LOG")))
    trace = evidence["build_info"][BUILD_ID]
    assert trace["request_count"] == 1
    assert trace["response_count"] == 4
    assert trace["status_changes"] == ["queued", "building", "succeeded"]
    assert trace["terminal_response_count"] == 1
    assert set(trace["last_terminal_build"]) == {"build_id", "appid", "jenkins_build_number", "status", "updated_at"}
    assert "PRIVATE" not in json.dumps(trace)


def test_valid_terminal_evidence_and_no_new_requests_pass(clock: Any) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)
    page.show_build(build())
    info_reply(page, build())
    final, checks = wait_for_terminal(page, evidence)
    assert final == build()
    assert all(checks.values())
    assert checks["build_info_request_observed"]
    assert checks["terminal_build_info_response_observed"]
    assert page.waits == [6500]


def test_css_uppercase_status_does_not_hide_the_terminal_state(clock: Any) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)
    page.show_build(build())
    info_reply(page, build())
    assert page.locator("#buildStatus").inner_text() == "SUCCEEDED"
    assert verifier.visible_build_status(page) == "succeeded"
    final, checks = wait_for_terminal(page, evidence)
    assert final["status"] == "succeeded"
    assert checks["visible_expected_terminal_status"]
    assert page.waits == [6500]


@pytest.mark.parametrize("output", ["invalid-json", "stale-build", "wrong-build"])
def test_terminal_response_with_missing_ui_reflection_has_a_short_failure_window(clock: Any, output: str) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)
    page.show_build(build("building"))
    if output == "invalid-json":
        page.texts["#buildOutput"] = "PRIVATE_ERROR not JSON"
    elif output == "wrong-build":
        page.texts["#buildOutput"] = json.dumps(build(build_id="OTHER-BUILD"))
    info_reply(page, build())
    with pytest.raises(verifier.BuildVerificationError) as caught:
        wait_for_terminal(page, evidence)
    assert caught.value.reason == "terminal_ui_not_reflected"
    assert caught.value.final == build()
    assert caught.value.checks["terminal_build_info_response_observed"]
    assert caught.value.checks["visible_expected_terminal_status"] is False
    assert clock.elapsed == verifier.TERMINAL_UI_GRACE_SECONDS


def test_safe_progress_reports_counts_without_ui_or_error_text(clock: Any, capsys: pytest.CaptureFixture[str]) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)
    page.show_build(build("building"))
    page.texts["#buildStatus"] = "PRIVATE_ERROR with credentials"
    info_reply(page, build("building", error="PRIVATE_ERROR"))
    with pytest.raises(verifier.BuildVerificationError):
        wait_for_terminal(page, evidence, timeout=1)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "build_poll status=unknown requests=1 responses=1 terminal_responses=0 elapsed_seconds=0" in captured.err
    assert "PRIVATE" not in captured.err
    assert BUILD_ID not in captured.err and APPID not in captured.err


def test_unreadable_ui_does_not_become_a_silent_terminal_timeout(clock: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)

    def fail(*args: Any) -> Any:
        raise verifier.PlaywrightError("PRIVATE_ERROR with request details")

    monkeypatch.setattr(page, "locator", fail)
    with pytest.raises(verifier.BuildVerificationError) as caught:
        wait_for_terminal(page, evidence)
    assert caught.value.reason == "ui_build_output_unreadable"
    assert "PRIVATE" not in str(caught.value)
    assert page.waits == []


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_unexpected_real_terminal_fails_immediately_with_safe_metadata(clock: Any, status: str) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)
    page.show_build(build("queued"))
    info_reply(page, build(status, error="PRIVATE_TOKEN", logs="PRIVATE_LOG"))
    with pytest.raises(verifier.BuildVerificationError) as caught:
        wait_for_terminal(page, evidence)
    assert caught.value.reason == "unexpected_terminal_status"
    assert caught.value.final == build(status)
    assert caught.value.checks["terminal_status_matches_expectation"] is False
    assert page.waits == []
    assert clock.elapsed == 0
    assert "PRIVATE" not in repr(caught.value)


@pytest.mark.parametrize("failure", ["aborted", "http-error"])
def test_requests_after_terminal_fail_stop_check_even_without_valid_responses(clock: Any, failure: str) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)
    page.show_build(build())
    info_reply(page, build())

    def retry(milliseconds: int) -> None:
        request = info_request(page)
        if failure == "http-error":
            page.handlers["response"](MockResponse(request, 503, {"error": "PRIVATE_ERROR"}))

    page.on_wait = retry
    with pytest.raises(verifier.BuildVerificationError) as caught:
        wait_for_terminal(page, evidence)
    assert caught.value.reason == "polling_continued_after_terminal"
    assert caught.value.checks["polling_stopped_after_terminal"] is False
    assert caught.value.final == build()
    assert evidence["build_info"][BUILD_ID]["request_count"] == 2
    assert evidence["build_info"][BUILD_ID]["terminal_response_count"] == 1


@pytest.mark.parametrize("response", ["none", "http-error", "wrong-app"])
def test_zero_or_failed_polling_cannot_pass_from_a_terminal_ui_snapshot(clock: Any, response: str) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)
    page.show_build(build())
    if response == "http-error":
        info_reply(page, build(), status=500)
    elif response == "wrong-app":
        info_reply(page, build(appid="OTHER-APP"))
    with pytest.raises(verifier.BuildVerificationError) as caught:
        wait_for_terminal(page, evidence)
    assert caught.value.reason == "terminal_evidence_missing"
    assert caught.value.checks["terminal_build_info_response_observed"] is False
    if response == "none":
        assert caught.value.checks["build_info_request_observed"] is False
    assert "polling_stopped_after_terminal" not in caught.value.checks
    assert page.waits == []


def test_timeout_preserves_last_safe_real_build(clock: Any) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)
    page.show_build(build("building"))
    info_reply(page, build("building", error="PRIVATE_ERROR"))
    with pytest.raises(verifier.BuildVerificationError) as caught:
        wait_for_terminal(page, evidence, timeout=1)
    assert caught.value.reason == "terminal_state_timeout"
    assert caught.value.final == build("building")
    assert clock.elapsed == 1


def prepare_case(monkeypatch: pytest.MonkeyPatch, page: MockPage, *, requests: int = 1, terminal: str = "succeeded") -> tuple[Any, list[Any]]:
    calls: list[Any] = []
    helpers = SimpleNamespace(create_app=lambda *args: calls.append(args))

    def submit(page: MockPage, appid: str, helpers: Any, git_ref: str) -> dict[str, Any]:
        assert git_ref == COMMIT
        for _ in range(requests):
            create_request(page, appid)
        page.show_build(build(terminal))
        info_reply(page, build(terminal, error="PRIVATE_ERROR", logs="PRIVATE_LOG"))
        return {"build_id": BUILD_ID, "appid": appid, "status": "queued"}

    monkeypatch.setattr(verifier, "submit_build_from_ui", submit)
    monkeypatch.setattr(verifier, "inspect_history_checks", lambda *args: {"history_terminal_status_matches": True})
    return helpers, calls


def run_case(page: MockPage, helpers: Any, evidence: dict[str, Any], **options: Any) -> dict[str, Any]:
    return verifier.run_case(
        page, helpers, evidence, name="success", appid=APPID, image="nginx:alpine",
        repository_url=REPOSITORY, git_ref=COMMIT, run="MOCK", expected_status="succeeded",
        timeout_seconds=900, **options,
    )


def test_case_forwards_repository_commit_and_optional_port_without_disclosing_them(monkeypatch: pytest.MonkeyPatch, clock: Any) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)
    helpers, calls = prepare_case(monkeypatch, page)
    result = run_case(page, helpers, evidence, published_port=18114)
    assert result["passed"]
    assert result["build_create_request_count"] == 1
    assert result["build_info_request_count"] == result["build_info_response_count"] == 1
    assert result["terminal_build_info_response_count"] == 1
    assert len(calls) == 1
    assert calls[0][3]["services"]["web"]["ports"] == ["18114:80"]
    assert calls[0][4]["repository_url"] == REPOSITORY
    assert calls[0][4]["git_ref"] == COMMIT
    serialized = json.dumps(result)
    assert REPOSITORY not in serialized and COMMIT not in serialized and "PRIVATE" not in serialized


@pytest.mark.parametrize("requests", [0, 2])
def test_case_requires_exactly_one_build_create_request(monkeypatch: pytest.MonkeyPatch, clock: Any, requests: int) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)
    helpers, _ = prepare_case(monkeypatch, page, requests=requests)
    result = run_case(page, helpers, evidence)
    assert result["passed"] is False
    assert result["error_stage"] == "submit_build"
    assert result["checks"]["exactly_one_build_create_request"] is False
    assert result["build_create_request_count"] == requests
    assert page.waits == []


def test_case_failure_reports_actual_terminal_not_automation_error(monkeypatch: pytest.MonkeyPatch, clock: Any) -> None:
    page = MockPage(clock)
    evidence = verifier.install_api_observer(page)
    helpers, _ = prepare_case(monkeypatch, page, terminal="failed")
    result = run_case(page, helpers, evidence)
    assert result["passed"] is False
    assert result["status"] == "failed"
    assert result["jenkins_build_number"] == 17
    assert result["updated_at"] == build()["updated_at"]
    assert result["error_stage"] == "automatic_polling"
    assert result["error_reason"] == "unexpected_terminal_status"
    assert result["status_changes"] == ["failed"]
    assert "PRIVATE" not in json.dumps(result)


def test_submit_build_sets_the_operator_git_ref() -> None:
    page = MockPage()
    receipt = {"appid": APPID, "build_id": BUILD_ID}
    helpers = SimpleNamespace(navigate_section=lambda *args: None, json_from_pre=lambda *args: receipt)
    assert verifier.submit_build_from_ui(page, APPID, helpers, COMMIT) == receipt
    assert page.fills["#buildGitRef"] == COMMIT
    assert page.clicks == ['#buildForm button[type="submit"]']


def test_compose_has_only_one_nginx_service_and_no_default_published_port() -> None:
    compose = verifier.release_compose("nginx:alpine")
    assert list(compose["services"]) == ["web"]
    assert compose["services"]["web"]["image"] == "nginx:alpine"
    assert "ports" not in compose["services"]["web"]


def test_cli_requires_repository_and_ref() -> None:
    with pytest.raises(SystemExit) as caught:
        verifier.parse_args([])
    assert caught.value.code == 2
    with pytest.raises(SystemExit):
        verifier.parse_args(["--repository-url", REPOSITORY])


@pytest.mark.parametrize(
    "override",
    [
        ["--repository-url", "   "],
        ["--git-ref", "   "],
        ["--source-image", "   "],
        ["--published-port", "0"],
        ["--published-port", "65536"],
        ["--timeout-seconds", "0"],
    ],
)
def test_cli_rejects_empty_sources_and_invalid_port_or_timeout(override: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        verifier.parse_args(["--repository-url", REPOSITORY, "--git-ref", COMMIT, *override])
    assert caught.value.code == 2


def test_cli_accepts_explicit_repository_ref_and_optional_port() -> None:
    options = verifier.parse_args(["--repository-url", REPOSITORY, "--git-ref", COMMIT])
    assert options.repository_url == REPOSITORY and options.git_ref == COMMIT
    assert options.published_port is None
    options = verifier.parse_args(["--repository-url", REPOSITORY, "--git-ref", COMMIT, "--published-port", "18114"])
    assert options.published_port == 18114


def test_main_creates_only_one_unique_case_and_never_performs_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    page = MockPage()
    closes: list[bool] = []
    browser = SimpleNamespace(new_page=lambda **kwargs: page, close=lambda: closes.append(True))
    engine = SimpleNamespace(chromium=SimpleNamespace(executable_path="mock-chromium", launch=lambda **kwargs: browser))

    class Context:
        def __enter__(self) -> Any:
            return engine

        def __exit__(self, *args: Any) -> None:
            pass

    helpers = SimpleNamespace(read_dotenv=lambda path: {}, connect=lambda *args: None)
    result_path = tmp_path / "result.json"
    options = verifier.parse_args(["--repository-url", REPOSITORY, "--git-ref", COMMIT, "--run", "MOCK", "--result", str(result_path)])
    calls: list[dict[str, Any]] = []

    def case(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"case": "success", "appid": kwargs["appid"], "passed": True}

    monkeypatch.setattr(verifier, "parse_args", lambda: options)
    monkeypatch.setattr(verifier, "live_delivery_helpers", lambda: helpers)
    monkeypatch.setattr(verifier, "sync_playwright", Context)
    monkeypatch.setattr(verifier, "run_case", case)
    assert verifier.main() == 0
    assert len(calls) == 1
    assert calls[0]["appid"].startswith("UIREL-")
    assert calls[0]["repository_url"] == REPOSITORY and calls[0]["git_ref"] == COMMIT
    output = json.loads(result_path.read_text(encoding="utf-8"))
    assert output["passed"] and len(output["cases"]) == 1
    assert REPOSITORY not in capsys.readouterr().out
    assert closes == [True]
