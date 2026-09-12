import json

import pytest

from orchestrator.gitlab import GitLabError, GitLabHttpAdapter, GitLabNotFoundError, HttpResponse


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
