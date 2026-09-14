import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from orchestrator.gitlab import GitLabError, GitLabHttpAdapter, GitLabNotFoundError, HttpResponse, UrllibHttpTransport


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def json_response(payload, status=200, headers=None):
    return HttpResponse(status_code=status, headers=headers or {}, body=json.dumps(payload).encode())


def test_validate_repository_ref_resolves_project_and_slash_ref() -> None:
    transport = FakeTransport(
        [
            json_response({"id": 44, "path_with_namespace": "platform/orders", "web_url": "https://gitlab.example/platform/orders"}),
            json_response({"id": "f" * 40}),
        ]
    )
    adapter = GitLabHttpAdapter("https://gitlab.example", "private-token", transport=transport)

    resolved = adapter.validate_repository_ref("git@gitlab.example:platform/orders.git", "release/2026.09")

    assert resolved.project.project_id == 44
    assert resolved.project.path_with_namespace == "platform/orders"
    assert resolved.git_ref == "release/2026.09"
    assert resolved.commit_sha == "f" * 40
    assert transport.calls[0]["url"] == "https://gitlab.example/api/v4/projects/platform%2Forders"
    assert transport.calls[1]["url"] == "https://gitlab.example/api/v4/projects/44/repository/commits/release%2F2026.09"
    assert transport.calls[0]["headers"]["PRIVATE-TOKEN"] == "private-token"
    assert "private-token" not in repr(adapter)


def test_validate_repository_ref_has_safe_not_found_and_transport_errors() -> None:
    token = "token-that-must-not-appear"
    missing = GitLabHttpAdapter(
        "https://gitlab.example",
        token,
        transport=FakeTransport([HttpResponse(status_code=404)]),
    )
    with pytest.raises(GitLabNotFoundError) as missing_error:
        missing.validate_repository_ref("https://gitlab.example/team/api.git", "main")
    assert token not in str(missing_error.value)

    failed = GitLabHttpAdapter(
        "https://gitlab.example",
        token,
        transport=FakeTransport([RuntimeError(f"network failure using {token}")]),
    )
    with pytest.raises(GitLabError) as transport_error:
        failed.validate_repository_ref("https://gitlab.example/team/api.git", "main")
    assert str(transport_error.value) == "GitLab request failed"
    assert token not in str(transport_error.value)


def test_validate_repository_ref_rejects_other_gitlab_hosts_before_request() -> None:
    transport = FakeTransport([])
    adapter = GitLabHttpAdapter("https://gitlab.example", "private-token", transport=transport)

    with pytest.raises(GitLabError, match="host does not match"):
        adapter.validate_repository_ref("https://other-gitlab.example/team/api.git", "main")

    assert transport.calls == []


def test_urllib_transport_reuses_set_cookie_for_follow_up_requests() -> None:
    seen_cookie: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            self.send_response(200)
            if self.path == "/crumb":
                self.send_header("Set-Cookie", "JSESSIONID=test-session; Path=/")
                body = b"crumb"
            else:
                seen_cookie.append(self.headers.get("Cookie"))
                body = b"build"
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = UrllibHttpTransport(timeout=2)
        base = f"http://127.0.0.1:{server.server_port}"
        first = transport.request("GET", f"{base}/crumb", headers={})
        second = transport.request("GET", f"{base}/build", headers={})
        assert first.status_code == second.status_code == 200
        assert seen_cookie == ["JSESSIONID=test-session"]
    finally:
        server.shutdown()
        thread.join(timeout=2)
