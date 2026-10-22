import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from threading import Thread
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import zlib

import grpc
from grpc_reflection.v1alpha import reflection
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
import httpx
import pytest

from orchestrator.route_probes import ProbeClient, ProbeEndpoint, validate_probe_payload


def http_payload(**changes):
    return {
        "method": "GET", "query_params": {}, "body": None,
        "assertions": {"expected_status_list": [200]}, **changes,
    }


@pytest.fixture
def http_service():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urlsplit(self.path)
            self.server.requests.append((self.command, self.path, dict(self.headers), b""))
            if path.path == "/slow":
                time.sleep(0.15)
            data = {
                "route": path.path, "query": parse_qs(path.query),
                "items": [{"state": "ready"}], "ok": True,
            }
            if path.path == "/large":
                body = b"x" * 100000
            elif path.path == "/invalid-json":
                body = b"not JSON"
            else:
                body = json.dumps(data).encode()
            self.send_response(503 if path.path == "/failed" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Probe", "ready")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.server.requests.append((self.command, self.path, dict(self.headers), body))
            response = json.dumps({"received": json.loads(body), "echo": self.headers.get("Authorization")}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Authorization", self.headers.get("Authorization", ""))
            self.send_header("Set-Cookie", "session=server-private")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            server=server,
            endpoint=ProbeEndpoint("web", "127.0.0.1", server.server_port, target_port=8000),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_real_io_sends_the_authoritative_route_and_all_assertion_kinds(http_service, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    payload = http_payload(query_params={"name": ["one", "two"], "q": "a b"}, assertions={
        "expected_status_list": [200], "expected_headers": {"X-Probe": "ready"},
        "body_contains": ["ready", "one"], "json_contains": {"ok": True},
        "json_path_equals": {"$.items[0].state": "ready", "query.name[1]": "two"},
    })
    result = ProbeClient().run(payload, require_route="/core", endpoints=[http_service.endpoint])
    assert result["passed"] is True
    assert result["response"]["json"]["route"] == "/core"
    assert result["response"]["headers"]["x-probe"] == "ready"
    assert result["assertion_errors"] == []
    request = http_service.server.requests[0]
    assert request[0] == "GET"
    assert request[1].startswith("/core?")
    assert parse_qs(urlsplit(request[1]).query) == {"name": ["one", "two"], "q": ["a b"]}


def test_http_post_json_and_authorization_are_sent_but_never_echoed_back(http_service):
    payload = http_payload(method="post", body={"message": "hello"}, headers={"Authorization": "Bearer private-access-token"}, assertions={
        "expected_status_list": [201], "json_path_equals": {"received.message": "hello"},
    })
    result = ProbeClient().run(payload, require_route="/core", endpoints=[http_service.endpoint])
    assert result["passed"] is True
    assert json.loads(http_service.server.requests[0][3]) == {"message": "hello"}
    assert {key.lower(): value for key, value in http_service.server.requests[0][2].items()}["authorization"] == "Bearer private-access-token"
    assert "private-access-token" not in json.dumps(result)
    assert "authorization" not in result["response"]["headers"]
    assert "set-cookie" not in result["response"]["headers"]


@pytest.mark.parametrize("route,error", [("/failed", "assertion_failed"), ("/slow", "timeout"), ("/large", "response_too_large")])
def test_http_real_failures_are_bounded_and_keep_safe_response_evidence(http_service, route, error):
    client = ProbeClient(timeout_seconds=0.03 if route == "/slow" else 1, max_response_bytes=128)
    result = client.run(http_payload(), require_route=route, endpoints=[http_service.endpoint])
    assert result["passed"] is False
    assert result["error_type"] == error
    if route != "/slow":
        assert result["response"]["body_bytes"] <= 128
    if route == "/large":
        assert result["response"]["truncated"] is True


@pytest.mark.parametrize("assertions", [
    {"expected_headers": {"x-probe": "wrong"}}, {"body_equals": "different"},
    {"body_contains": "absent"}, {"json_equals": {"ok": False}},
    {"json_contains": {"items": [{"state": "wrong"}]}},
    {"json_path_equals": {"items[5].state": "ready"}},
    {"json_path_equals": {"items.state": "ready"}},
])
def test_http_status_alone_does_not_override_other_failed_assertions(http_service, assertions):
    payload = http_payload(assertions={"expected_status_list": [200], **assertions})
    result = ProbeClient().run(payload, require_route="/core", endpoints=[http_service.endpoint])
    assert result["passed"] is False
    assert result["error_type"] == "assertion_failed"
    assert result["assertion_errors"]
    assert result["response"]["status_code"] == 200


def test_http_non_json_response_cannot_pass_json_assertions(http_service):
    result = ProbeClient().run(http_payload(assertions={"expected_status_list": [200], "json_equals": None}), require_route="/invalid-json", endpoints=[http_service.endpoint])
    assert result["passed"] is False
    assert result["response"]["body"] == "not JSON"


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
def test_http_compressed_response_is_decoded_with_a_bounded_expansion(encoding, compress):
    def handle(_request):
        return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=httpx.ByteStream(compress(b'{"ok":true}')))

    client = ProbeClient(transport=httpx.MockTransport(handle))
    payload = http_payload(assertions={"expected_status_list": [200], "json_equals": {"ok": True}})
    result = client.run(payload, require_route="/core", endpoints=[ProbeEndpoint("web", "owned.example", 80)])
    assert result["passed"] is True
    assert result["response"]["body"] == '{"ok":true}'


def test_http_compressed_bomb_cannot_bypass_the_response_byte_limit():
    encoded = gzip.compress(b"x" * 2 * 1024 * 1024)

    def handle(_request):
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(encoded))

    result = ProbeClient(max_response_bytes=4096, transport=httpx.MockTransport(handle)).run(http_payload(), require_route="/core", endpoints=[ProbeEndpoint("web", "owned.example", 80)])
    assert result["passed"] is False
    assert result["error_type"] == "response_too_large"
    assert result["response"]["body_bytes"] == 4096


@pytest.mark.parametrize("encoding,data", [("unknown", b"data"), ("gzip", b"not a gzip frame"), ("gzip", gzip.compress(b"complete")[:-2])])
def test_http_unsupported_or_corrupt_content_encoding_fails_safely(encoding, data):
    def handle(_request):
        return httpx.Response(200, headers={"content-encoding": encoding}, stream=httpx.ByteStream(data))

    result = ProbeClient(transport=httpx.MockTransport(handle)).run(http_payload(), require_route="/core", endpoints=[ProbeEndpoint("web", "owned.example", 80)])
    assert result["passed"] is False
    assert result["error_type"] == "invalid_response"


def test_json_assertions_distinguish_booleans_from_numbers_at_every_depth():
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"nested": {"ok": True}}))
    result = ProbeClient(transport=transport).run(http_payload(assertions={"expected_status_list": [200], "json_equals": {"nested": {"ok": 1}}}), require_route="/core", endpoints=[ProbeEndpoint("web", "owned.example", 80)])
    assert result["passed"] is False
    assert result["assertion_errors"] == ["Response JSON equality assertion failed"]


@pytest.mark.parametrize("assertion", ["json_equals", "json_contains", "json_path_equals"])
@pytest.mark.parametrize("actual,expected,matches", [
    (True, 1, False), (1, True, False), (False, 0, False), (0, False, False),
    (True, 1.0, False), (0.0, False, False),
    ({"items": [{"ready": True}]}, {"items": [{"ready": 1}]}, False),
    ({"items": [{"ready": 1}]}, {"items": [{"ready": True}]}, False),
    ([False, {"nested": [True]}], [0, {"nested": [1]}], False),
    ([0, {"nested": [1]}], [False, {"nested": [True]}], False),
    ({"items": [{"ready": True, "absent": None}]}, {"items": [{"ready": True, "absent": None}]}, True),
    (1, 1.0, True), (1.0, 1, True),
    ({"items": [{"count": 1.0}]}, {"items": [{"count": 1}]}, True),
    ([0, {"nested": [2.0]}], [0.0, {"nested": [2]}], True),
    ({"value": None}, {"value": False}, False),
])
def test_all_json_assertions_use_json_types_recursively(assertion, actual, expected, matches):
    expected_value = {"$": expected} if assertion == "json_path_equals" else expected
    request = http_payload(assertions={"expected_status_list": [200], assertion: expected_value})
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=actual))
    result = ProbeClient(transport=transport).run(request, require_route="/core", endpoints=[ProbeEndpoint("web", "owned.example", 80)])
    assert result["passed"] is matches
    assert bool(result["assertion_errors"]) is (not matches)


@pytest.mark.parametrize("actual,expected,matches", [(True, 1, False), (1, True, False), (1.0, 1, True), (False, 0.0, False), (None, None, True)])
def test_json_path_equality_keeps_types_for_values_selected_through_arrays(actual, expected, matches):
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"items": [{"nested": {"value": actual}}]}))
    request = http_payload(assertions={"expected_status_list": [200], "json_path_equals": {"$.items[0].nested.value": expected}})
    result = ProbeClient(transport=transport).run(request, require_route="/core", endpoints=[ProbeEndpoint("web", "owned.example", 80)])
    assert result["passed"] is matches


@pytest.mark.parametrize("body", ["NaN", "Infinity", "-Infinity", '{"value": NaN}', '{"value": 1e9999}', '{"value": "\\ud800"}'])
def test_http_json_values_must_be_finite_and_safe_for_utf8_result_serialization(body):
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=body))
    request = http_payload(assertions={"expected_status_list": [200], "json_path_equals": {"$": {}}})
    result = ProbeClient(transport=transport).run(request, require_route="/core", endpoints=[ProbeEndpoint("web", "owned.example", 80)])
    assert result["passed"] is False
    assert "json" not in result["response"]
    assert result["response"]["body"] == body
    json.dumps(result, allow_nan=False)


def test_http_does_not_follow_redirects_or_reuse_response_cookies():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://unowned.example/private", "set-cookie": "secret=private"})

    client = ProbeClient(transport=httpx.MockTransport(handle))
    endpoint = ProbeEndpoint("web", "owned.example", 80)
    result = client.run(http_payload(), require_route="/core", endpoints=[endpoint])
    assert result["passed"] is False
    assert len(calls) == 1
    assert str(calls[0].url) == "http://owned.example/core"
    assert "cookie" not in calls[0].headers
    result = client.run(http_payload(assertions={"expected_status_list": [302]}), require_route="/core", endpoints=[endpoint])
    assert result["passed"] is True
    assert len(calls) == 2
    assert "cookie" not in calls[1].headers


def test_http_tries_only_owned_candidates_and_selectors_cannot_supply_an_address():
    calls = []

    def handle(request):
        calls.append(request.url.host)
        return httpx.Response(200 if request.url.host == "working.example" else 503, json={"ok": True})

    client = ProbeClient(transport=httpx.MockTransport(handle))
    endpoints = [ProbeEndpoint("db", "failed.example", 18000, target_port=5432), ProbeEndpoint("web", "working.example", 18001, target_port=8000)]
    result = client.run(http_payload(), require_route="/core", endpoints=endpoints)
    assert result["passed"] is True
    assert len(result["attempts"]) == 2
    assert calls == ["failed.example", "working.example"]
    calls.clear()
    result = client.run(http_payload(service_name="web", target_port=8000), require_route="/core", endpoints=endpoints)
    assert result["passed"] is True
    assert calls == ["working.example"]
    calls.clear()
    result = client.run(http_payload(target_port=18001), require_route="/core", endpoints=endpoints)
    assert result["error_type"] == "no_endpoint"
    assert calls == []


@pytest.mark.parametrize("route", ["https://unowned.example/core", "//unowned.example/core", "/\\unowned.example/core", "/%2funowned.example/core", "/../core", "/%252e%252e/core", "/core?target=other", "/core#fragment", "/core\r\n"])
def test_unsafe_routes_fail_before_network_io(route):
    def handle(_request):
        pytest.fail("Invalid route reached a transport")

    result = ProbeClient(transport=httpx.MockTransport(handle)).run(http_payload(), require_route=route, endpoints=[ProbeEndpoint("web", "owned.example", 80)])
    assert result["error_type"] == "invalid_payload"


@pytest.mark.parametrize("changes", [
    {"url": "http://unowned.example"}, {"host": "unowned.example"},
    {"path": "/different"}, {"headers": {"Host": "unowned.example"}},
    {"headers": {"Content-Length": "9999"}}, {"headers": {"x-extra": "value\r\nInjected: bad"}},
    {"method": "CONNECT"}, {"query_params": {"x": {"nested": "invalid"}}},
    {"assertions": {"expected_status_list": []}}, {"assertions": {"expected_status_list": [True]}},
    {"assertions": {"expected_status_list": [200], "ignored_assertion": True}},
    {"assertions": {"expected_status_list": [200], "json_path_equals": {"x..value": 1}}},
    {"protocol": []}, {"target_port": True}, {"body": float("inf")},
])
def test_payload_validation_rejects_ignored_or_unsafe_agent_options(changes):
    with pytest.raises(ValueError, match="Invalid probe payload"):
        validate_probe_payload(http_payload(**changes), "/core")


@pytest.mark.parametrize("removed", ["method", "query_params", "body", "assertions"])
def test_http_complete_payload_is_mandatory(removed):
    payload = http_payload()
    payload.pop(removed)
    with pytest.raises(ValueError):
        validate_probe_payload(payload, "/core")


def test_payload_validation_returns_an_independent_normalized_copy():
    payload = http_payload(method="get", path="/core", headers={"X-Probe": "ready"}, assertions={"expected_status_list": [200], "body_contains": "ready"})
    original = deepcopy(payload)
    normalized = validate_probe_payload(payload, "/core")
    assert payload == original
    assert normalized["method"] == "GET"
    assert normalized["headers"] == {"x-probe": "ready"}
    assert normalized["assertions"]["body_contains"] == ["ready"]


def test_transport_exception_values_are_never_exposed():
    def handle(request):
        raise httpx.ReadTimeout("tcp://user:private-password@daemon; Authorization: private-access-token", request=request)

    result = ProbeClient(transport=httpx.MockTransport(handle)).run(http_payload(), require_route="/core", endpoints=[ProbeEndpoint("web", "owned.example", 80)])
    assert result["error_type"] == "timeout"
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize("options", [{"timeout_seconds": 0}, {"timeout_seconds": True}, {"timeout_seconds": float("inf")}, {"max_response_bytes": 0}, {"max_response_bytes": True}])
def test_probe_client_limits_must_be_valid(options):
    with pytest.raises(ValueError):
        ProbeClient(**options)


@pytest.mark.parametrize("changes", [{"host": "http://unowned.example"}, {"host": "host@unowned.example"}, {"host": "unowned.example/path"}, {"port": True}, {"port": 65536}, {"scheme": "ftp"}, {"target_port": 0}])
def test_endpoint_constructor_rejects_address_injection(changes):
    with pytest.raises(ValueError):
        ProbeEndpoint(**{"service_name": "web", "host": "owned.example", "port": 80, **changes})


def _proto_contract():
    file = descriptor_pb2.FileDescriptorProto(name="route_probe_test.proto", package="routechecks", syntax="proto3")
    request = file.message_type.add(name="EchoRequest")
    request.field.add(name="message", number=1, type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    request.field.add(name="mode", number=2, type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    response = file.message_type.add(name="EchoReply")
    response.field.add(name="message", number=1, type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    response.field.add(name="ready", number=2, type=descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)
    details = file.message_type.add(name="ReadinessInfo")
    details.field.add(name="ready", number=1, type=descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)
    details.field.add(name="count", number=2, type=descriptor_pb2.FieldDescriptorProto.TYPE_INT32)
    details.field.add(name="flags", number=3, type=descriptor_pb2.FieldDescriptorProto.TYPE_BOOL, label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED)
    response.field.add(name="details", number=3, type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, type_name=".routechecks.ReadinessInfo")
    service = file.service.add(name="EchoService")
    service.method.add(name="Echo", input_type=".routechecks.EchoRequest", output_type=".routechecks.EchoReply")
    service.method.add(name="Stream", input_type=".routechecks.EchoRequest", output_type=".routechecks.EchoReply", server_streaming=True)
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(file.SerializeToString())
    descriptor_set = descriptor_pb2.FileDescriptorSet()
    descriptor_set.file.add().CopyFrom(file)
    return SimpleNamespace(
        pool=pool, descriptor_set_base64=base64.b64encode(descriptor_set.SerializeToString()).decode("ascii"),
        request=message_factory.GetMessageClass(pool.FindMessageTypeByName("routechecks.EchoRequest")),
        response=message_factory.GetMessageClass(pool.FindMessageTypeByName("routechecks.EchoReply")),
    )


@pytest.fixture
def grpc_service():
    contract = _proto_contract()
    calls = []

    def echo(request, context):
        calls.append((request, dict(context.invocation_metadata())))
        if request.mode == "missing":
            context.abort(grpc.StatusCode.NOT_FOUND, "Requested item does not exist")
        if request.mode == "slow":
            time.sleep(0.15)
        context.send_initial_metadata((("x-probe", "ready"), ("authorization", "server-private")))
        context.set_trailing_metadata((("x-trace", "finished"),))
        result = contract.response(message="x" * 100000 if request.mode == "large" else request.message, ready=True)
        if request.mode == "typed":
            result.details.ready = True
            result.details.count = 1
            result.details.flags.extend([False, True])
        return result

    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    handlers = {
        "Echo": grpc.unary_unary_rpc_method_handler(echo, request_deserializer=contract.request.FromString, response_serializer=contract.response.SerializeToString),
        "Stream": grpc.unary_stream_rpc_method_handler(lambda request, ctx: iter([echo(request, ctx)]), request_deserializer=contract.request.FromString, response_serializer=contract.response.SerializeToString),
    }
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("routechecks.EchoService", handlers),))
    reflection.enable_server_reflection(("routechecks.EchoService", reflection.SERVICE_NAME), server, pool=contract.pool)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield SimpleNamespace(
            contract=contract, calls=calls, server=server,
            endpoint=ProbeEndpoint("rpc", "127.0.0.1", port, scheme="grpc", target_port=50051),
        )
    finally:
        server.stop(0).wait(timeout=2)


def grpc_payload(service, **changes):
    return {
        "protocol": "grpc", "body": {"message": "hello"},
        "descriptor_set_base64": service.contract.descriptor_set_base64,
        "assertions": {"expected_grpc_status_list": ["OK"]}, **changes,
    }


def test_grpc_real_unary_io_with_dynamic_descriptors_and_json_assertions(grpc_service):
    payload = grpc_payload(grpc_service, assertions={
        "expected_grpc_status_list": [0], "expected_headers": {"x-probe": "ready", "x-trace": "finished"},
        "json_equals": {"message": "hello", "ready": True}, "json_path_equals": {"$.ready": True},
    })
    result = ProbeClient().run(payload, require_route="/routechecks.EchoService/Echo", endpoints=[grpc_service.endpoint])
    assert result["passed"] is True
    assert result["response"]["grpc_status"] == "OK"
    assert result["response"]["grpc_status_code"] == 0
    assert result["response"]["json"] == {"message": "hello", "ready": True}
    assert "authorization" not in result["response"]["headers"]
    assert grpc_service.contract.response.FromString(base64.b64decode(result["response"]["body_base64"])).message == "hello"
    assert len(grpc_service.calls) == 1


def test_grpc_real_reflection_resolves_protobuf_without_generated_code(grpc_service):
    payload = grpc_payload(grpc_service)
    payload.pop("descriptor_set_base64")
    result = ProbeClient().run(payload, require_route="/routechecks.EchoService/Echo", endpoints=[grpc_service.endpoint])
    assert result["passed"] is True
    assert result["response"]["json"]["message"] == "hello"


@pytest.mark.parametrize("assertion", ["json_equals", "json_contains", "json_path_equals"])
@pytest.mark.parametrize("case", ["boolean_as_number", "number_as_boolean", "json_number_equivalence"])
def test_grpc_reflection_json_assertions_keep_nested_boolean_and_numeric_types(grpc_service, assertion, case):
    actual = {"message": "hello", "ready": True, "details": {"ready": True, "count": 1, "flags": [False, True]}}
    expected = deepcopy(actual)
    if case == "boolean_as_number":
        expected["details"]["flags"][1] = 1
        subset = {"details": {"flags": [False, 1]}}
        paths = {"$.details.flags[1]": 1}
    elif case == "number_as_boolean":
        expected["details"]["count"] = True
        subset = {"details": {"count": True}}
        paths = {"details.count": True}
    else:
        expected["details"]["count"] = 1.0
        subset = {"details": {"count": 1.0}}
        paths = {"$.details.count": 1.0}
    expected_value = {"json_equals": expected, "json_contains": subset, "json_path_equals": paths}[assertion]
    request = grpc_payload(grpc_service, body={"message": "hello", "mode": "typed"}, assertions={"expected_grpc_status_list": ["OK"], assertion: expected_value})
    request.pop("descriptor_set_base64")
    result = ProbeClient().run(request, require_route="/routechecks.EchoService/Echo", endpoints=[grpc_service.endpoint])
    assert result["response"]["json"] == actual
    assert result["response"]["grpc_status"] == "OK"
    assert result["passed"] is (case == "json_number_equivalence")


def test_grpc_real_raw_base64_mode_supports_servers_without_descriptors(grpc_service):
    payload = {
        "protocol": "grpc", "body_base64": base64.b64encode(grpc_service.contract.request(message="opaque").SerializeToString()).decode("ascii"),
        "assertions": {"expected_grpc_status_list": ["OK"]},
    }
    result = ProbeClient().run(payload, require_route="/routechecks.EchoService/Echo", endpoints=[grpc_service.endpoint])
    assert result["passed"] is True
    decoded = grpc_service.contract.response.FromString(base64.b64decode(result["response"]["body_base64"]))
    assert decoded.message == "opaque"
    assert "json" not in result["response"]


def test_grpc_expected_non_ok_status_is_a_valid_assertion(grpc_service):
    payload = grpc_payload(grpc_service, body={"mode": "missing"}, assertions={"expected_grpc_status_list": [5]})
    result = ProbeClient().run(payload, require_route="/routechecks.EchoService/Echo", endpoints=[grpc_service.endpoint])
    assert result["passed"] is True
    assert result["response"]["grpc_status"] == "NOT_FOUND"
    assert result["response"]["grpc_status_code"] == 5
    assert "does not exist" in result["response"]["details"]
    payload["assertions"]["expected_grpc_status_list"] = ["OK"]
    result = ProbeClient().run(payload, require_route="/routechecks.EchoService/Echo", endpoints=[grpc_service.endpoint])
    assert result["passed"] is False
    assert result["error_type"] == "assertion_failed"


@pytest.mark.parametrize("mode,error", [("slow", "timeout"), ("large", "response_too_large")])
def test_grpc_real_timeout_and_response_size_limits(grpc_service, mode, error):
    payload = grpc_payload(grpc_service, body={"mode": mode})
    result = ProbeClient(timeout_seconds=0.03 if mode == "slow" else 1, max_response_bytes=64).run(payload, require_route="/routechecks.EchoService/Echo", endpoints=[grpc_service.endpoint])
    assert result["passed"] is False
    assert result["error_type"] == error
    assert len(base64.b64decode(result["response"]["body_base64"])) <= 64


def test_grpc_authorization_is_not_exposed_in_json_raw_bytes_or_metadata(grpc_service):
    payload = grpc_payload(grpc_service, body={"message": "private-access-token"}, headers={"Authorization": "Bearer private-access-token"})
    result = ProbeClient().run(payload, require_route="/routechecks.EchoService/Echo", endpoints=[grpc_service.endpoint])
    assert result["passed"] is True
    assert grpc_service.calls[0][1]["authorization"] == "Bearer private-access-token"
    assert "private-access-token" not in json.dumps(result)
    assert b"private-access-token" not in base64.b64decode(result["response"]["body_base64"])


@pytest.mark.parametrize("changes", [
    {"assertions": {"expected_grpc_status_list": []}}, {"assertions": {"expected_grpc_status_list": [17]}},
    {"assertions": {"expected_grpc_status_list": [True]}}, {"assertions": {"expected_grpc_status_list": ["nonexistent"]}},
    {"descriptor_set_base64": "invalid!!!"}, {"descriptor_set_base64": ""},
    {"reflection": False}, {"headers": {"host": "unowned.example"}},
    {"headers": {"custom-bin": "not binary"}}, {"query_params": {"target": "other"}},
])
def test_grpc_invalid_protocol_options_are_rejected(changes):
    payload = {"protocol": "grpc", "body": {}, "assertions": {"expected_grpc_status_list": ["OK"]}, **changes}
    with pytest.raises(ValueError):
        validate_probe_payload(payload, "/routechecks.EchoService/Echo")


def test_grpc_streaming_or_different_methods_cannot_bypass_the_required_route(grpc_service):
    for route in ("/routechecks.EchoService/Stream", "/routechecks.EchoService/Missing"):
        result = ProbeClient().run(grpc_payload(grpc_service), require_route=route, endpoints=[grpc_service.endpoint])
        assert result["error_type"] == "invalid_payload"
    assert grpc_service.calls == []


def test_grpc_invalid_protobuf_json_cannot_produce_a_successful_probe(grpc_service):
    result = ProbeClient().run(grpc_payload(grpc_service, body={"unsupported_field": True}), require_route="/routechecks.EchoService/Echo", endpoints=[grpc_service.endpoint])
    assert result["passed"] is False
    assert result["error_type"] == "invalid_payload"
    assert grpc_service.calls == []


def test_grpc_secure_scheme_uses_real_tls_and_verifies_server_identity(monkeypatch):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    certificate = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False).sign(private_key, hashes.SHA256())
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    contract = _proto_contract()
    server = grpc.server(ThreadPoolExecutor(max_workers=1))
    handler = grpc.unary_unary_rpc_method_handler(lambda request, _context: contract.response(message=request.message, ready=True), request_deserializer=contract.request.FromString, response_serializer=contract.response.SerializeToString)
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("routechecks.EchoService", {"Echo": handler}),))
    port = server.add_secure_port("127.0.0.1:0", grpc.ssl_server_credentials(((key_pem, cert_pem),)))
    server.start()
    try:
        payload = {"protocol": "grpc", "body": {"message": "TLS"}, "descriptor_set_base64": contract.descriptor_set_base64, "assertions": {"expected_grpc_status_list": ["OK"]}}
        endpoint = ProbeEndpoint("rpc", "127.0.0.1", port, scheme="grpcs")
        untrusted = ProbeClient(timeout_seconds=1).run(payload, require_route="/routechecks.EchoService/Echo", endpoints=[endpoint])
        assert untrusted["passed"] is False
        assert untrusted["response"]["grpc_status"] == "UNAVAILABLE"
        original_credentials = grpc.ssl_channel_credentials
        monkeypatch.setattr(grpc, "ssl_channel_credentials", lambda: original_credentials(root_certificates=cert_pem))
        trusted = ProbeClient(timeout_seconds=1).run(payload, require_route="/routechecks.EchoService/Echo", endpoints=[endpoint])
        assert trusted["passed"] is True
        assert trusted["response"]["json"]["message"] == "TLS"
    finally:
        server.stop(0).wait(timeout=2)
