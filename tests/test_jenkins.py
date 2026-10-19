import json
from urllib.parse import parse_qs

import pytest

from orchestrator.gitlab import HttpResponse
from orchestrator.jenkins import (
    JenkinsArtifactError,
    JenkinsBuildRequest,
    JenkinsBuildState,
    JenkinsError,
    JenkinsHttpAdapter,
    JenkinsNotStartedError,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body=None, max_bytes=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body, "max_bytes": max_bytes})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def json_response(payload, status=200, headers=None):
    return HttpResponse(status_code=status, headers=headers or {}, body=json.dumps(payload).encode())


def request() -> JenkinsBuildRequest:
    return JenkinsBuildRequest(
        appid="orders",
        repository_url="https://gitlab.example/platform/orders.git",
        git_ref="release/2026.09",
        environment={"TOKEN": "do-not-log", "MODE": "production"},
        compose={"services": {"api": {"image": "example/orders", "environment": {"TOKEN": "do-not-log"}}}},
        image_repository="harbor.example/apps/orders",
    )


def test_resource_artifact_success_and_missing_report():
    report = {"build_id": "a" * 32, "images": [], "verification_complete": True, "workspace_removed": True}
    transport = FakeTransport([json_response(report), HttpResponse(404)])
    adapter = JenkinsHttpAdapter("https://jenkins.example", "worker", "secret", transport=transport)
    assert adapter.get_resource_artifact(12) == report
    assert transport.calls[0]["url"].endswith("/12/artifact/orchestrator-resources.json")
    with pytest.raises(JenkinsArtifactError, match="missing"):
        adapter.get_resource_artifact(12)


def test_build_request_sends_platform_build_identity():
    from dataclasses import replace

    value = replace(request(), build_id="a" * 32)
    assert value.as_parameters()["BUILD_ID"] == "a" * 32


@pytest.mark.parametrize("responses, expected_calls", [([HttpResponse(401)], 1), ([HttpResponse(404), HttpResponse(403)], 2)])
def test_definitely_unstarted_jenkins_trigger_is_distinguished(responses, expected_calls):
    transport = FakeTransport(responses)
    adapter = JenkinsHttpAdapter("https://jenkins.example", "worker", "secret", transport=transport)
    with pytest.raises(JenkinsNotStartedError):
        adapter.trigger_build(request())
    assert len(transport.calls) == expected_calls


def test_trigger_poll_build_and_fetch_result_artifact() -> None:
    transport = FakeTransport(
        [
            json_response({"crumbRequestField": "Jenkins-Crumb", "crumb": "crumb-value"}),
            HttpResponse(status_code=201, headers={"Location": "/queue/item/21/"}),
            json_response({"id": 21, "cancelled": False, "executable": {"number": 18}}),
            json_response({"building": False, "result": "SUCCESS", "url": "https://jenkins.example/job/folder/job/apps/18/"}),
            json_response(
                {
                    "images": ["harbor.example/apps/orders:18"],
                    "compose": {"services": {"api": {"ports": ["18080:8080"]}}},
                }
            ),
        ]
    )
    adapter = JenkinsHttpAdapter(
        "https://jenkins.example",
        "worker",
        "super-secret",
        job_name="folder/apps",
        transport=transport,
    )

    queued = adapter.trigger_build(request())
    queue_item = adapter.get_queue_item(queued.queue_url)
    build = adapter.get_build(queue_item.build_number)
    artifact = adapter.get_result_artifact(build.number)

    assert queued.queue_url == "https://jenkins.example/queue/item/21/"
    assert queued.queue_id == 21
    assert queue_item.build_number == 18
    assert not queue_item.pending
    assert build.state is JenkinsBuildState.SUCCEEDED
    assert build.terminal
    assert artifact.images == ("harbor.example/apps/orders:18",)
    assert artifact["compose"] == {"services": {"api": {"ports": ["18080:8080"]}}}
    assert artifact.as_dict()["images"] == ["harbor.example/apps/orders:18"]
    assert artifact == {
        "images": ["harbor.example/apps/orders:18"],
        "compose": {"services": {"api": {"ports": ["18080:8080"]}}},
    }

    assert [call["url"] for call in transport.calls] == [
        "https://jenkins.example/crumbIssuer/api/json",
        "https://jenkins.example/job/folder/job/apps/buildWithParameters",
        "https://jenkins.example/queue/item/21/api/json",
        "https://jenkins.example/job/folder/job/apps/18/api/json",
        "https://jenkins.example/job/folder/job/apps/18/artifact/orchestrator-result.json",
    ]
    submitted = parse_qs(transport.calls[1]["body"].decode())
    assert submitted == {
        "APPID": ["orders"],
        "REPOSITORY_URL": ["https://gitlab.example/platform/orders.git"],
        "GIT_REF": ["release/2026.09"],
        "ENVIRONMENT_JSON": ['{"MODE":"production","TOKEN":"do-not-log"}'],
        "COMPOSE_JSON": ['{"services":{"api":{"environment":{"TOKEN":"do-not-log"},"image":"example/orders"}}}'],
        "IMAGE_REPOSITORY": ["harbor.example/apps/orders"],
        "HARBOR_REGISTRY": ["harbor.example"],
        "CHECKOUT_REQUIRED": ["false"],
    }
    assert transport.calls[1]["headers"]["Jenkins-Crumb"] == "crumb-value"
    assert transport.calls[1]["headers"]["Authorization"].startswith("Basic ")
    assert "super-secret" not in repr(adapter)
    assert "do-not-log" not in repr(request())


def test_queue_pending_and_cancelled_are_explicit() -> None:
    transport = FakeTransport(
        [
            json_response({"id": 22, "cancelled": False, "why": "Waiting for executor"}),
            json_response({"id": 23, "cancelled": True, "why": None}),
        ]
    )
    adapter = JenkinsHttpAdapter("https://jenkins.example", "worker", "secret", transport=transport)

    pending = adapter.get_queue_item("/queue/item/22/")
    cancelled = adapter.get_queue_item("https://jenkins.example/queue/item/23/")

    assert pending.pending
    assert pending.why == "Waiting for executor"
    assert not cancelled.pending
    assert cancelled.cancelled


def test_missing_or_invalid_result_artifact_fails_without_exposing_credentials() -> None:
    password = "must-not-appear"
    missing = JenkinsHttpAdapter(
        "https://jenkins.example",
        "worker",
        password,
        transport=FakeTransport([HttpResponse(status_code=404)]),
    )
    with pytest.raises(JenkinsArtifactError) as missing_error:
        missing.get_result_artifact(9)
    assert str(missing_error.value) == "Jenkins result artifact is missing"
    assert password not in str(missing_error.value)

    invalid = JenkinsHttpAdapter(
        "https://jenkins.example",
        "worker",
        password,
        transport=FakeTransport([json_response({"images": [], "compose": {}})]),
    )
    with pytest.raises(JenkinsArtifactError, match="invalid format"):
        invalid.get_result_artifact(9)


def test_trigger_allows_no_crumb_but_rejects_external_queue_locations() -> None:
    transport = FakeTransport(
        [
            HttpResponse(status_code=404),
            HttpResponse(status_code=201, headers={"Location": "https://untrusted.example/queue/item/1/"}),
        ]
    )
    adapter = JenkinsHttpAdapter("https://jenkins.example", "worker", "secret", transport=transport)

    with pytest.raises(JenkinsError, match="outside the configured host"):
        adapter.trigger_build(request())

    assert "Jenkins-Crumb" not in transport.calls[1]["headers"]


def test_transport_errors_are_reduced_to_safe_jenkins_error() -> None:
    password = "super-secret"
    adapter = JenkinsHttpAdapter(
        "https://jenkins.example",
        "worker",
        password,
        transport=FakeTransport([RuntimeError(f"failed with {password}")]),
    )

    with pytest.raises(JenkinsError) as error:
        adapter.get_build(4)

    assert str(error.value) == "Jenkins request failed"
    assert password not in str(error.value)


def test_failure_summary_reads_only_a_bounded_console_tail_and_redacts_credentials():
    size = 2 * 1024 * 1024
    console = (
        "[Pipeline] { (Checkout)\n"
        "fatal: unable to access 'https://oauth2:repository-secret@gitlab.example/team/app.git?token=query-secret': "
        "Could not resolve host: gitlab.example; API_TOKEN=inline-secret; adapter-password\n"
        "ERROR: script returned exit code 128\n"
    )
    transport = FakeTransport([
        HttpResponse(200, headers={"X-Text-Size": str(size)}),
        HttpResponse(200, body=console.encode()),
    ])
    adapter = JenkinsHttpAdapter("https://jenkins.example/ci", "worker", "adapter-password", job_name="folder/apps", transport=transport)

    summary = adapter.get_failure_summary(67, sensitive_values=["repository-secret"])

    assert "阶段=Checkout" in summary
    assert "DNS 解析失败" in summary
    assert "gitlab.example" in summary
    assert "退出码=128" in summary
    assert f"字节起点 {size - 128 * 1024}" in summary
    for value in ("repository-secret", "query-secret", "inline-secret", "adapter-password"):
        assert value not in summary
    assert transport.calls[0]["method"] == "HEAD"
    assert transport.calls[0]["max_bytes"] == 0
    assert transport.calls[1]["url"] == f"https://jenkins.example/ci/job/folder/job/apps/67/logText/progressiveText?start={size - 128 * 1024}"
    assert transport.calls[1]["max_bytes"] == 256 * 1024
    assert transport.calls[1]["headers"]["Accept"] == "text/plain"


@pytest.mark.parametrize("metadata", [HttpResponse(405), HttpResponse(501), HttpResponse(200), HttpResponse(200, headers={"X-Text-Size": "invalid"})])
def test_failure_summary_falls_back_to_a_bounded_prefix_when_size_is_unavailable(metadata):
    transport = FakeTransport([metadata, HttpResponse(200, body=b"fatal: Could not resolve host: gitlab.example\n")])
    adapter = JenkinsHttpAdapter("https://jenkins.example", "worker", "secret", transport=transport)
    assert "console L1" in adapter.get_failure_summary(5)
    assert transport.calls[1]["url"].endswith("/5/logText/progressiveText?start=0")
    assert transport.calls[1]["max_bytes"] == 256 * 1024


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_failure_summary_log_access_errors_do_not_surface_the_response_body(status):
    adapter = JenkinsHttpAdapter("https://jenkins.example", "worker", "secret", transport=FakeTransport([HttpResponse(status, body=b"password=do-not-expose")]))
    with pytest.raises(JenkinsError) as error:
        adapter.get_failure_summary(5)
    assert "console log" in str(error.value)
    assert "do-not-expose" not in str(error.value)


def test_failure_summary_does_not_request_arbitrary_build_urls_or_nonpositive_build_numbers():
    transport = FakeTransport([])
    adapter = JenkinsHttpAdapter("https://jenkins.example", "worker", "secret", transport=transport)
    with pytest.raises(ValueError):
        adapter.get_failure_summary(0)
    assert transport.calls == []
