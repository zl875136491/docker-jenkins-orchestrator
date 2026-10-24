"""Bounded HTTP/gRPC probes against deployment-owned endpoints.

Agent output defines a request, never its destination.  ``require_route`` and
``ProbeEndpoint`` objects are supplied by the deployment controller.  HTTP
payloads require ``method``, ``query_params``, ``body`` and
``assertions.expected_status_list``.  A gRPC payload uses ``protocol=grpc``,
``body`` (Protobuf JSON) and ``assertions.expected_grpc_status_list``; message
descriptors come from ``descriptor_set_base64`` or bounded server reflection.
``body_base64`` instead enables an opaque, unary Protobuf request/response.

Optional assertions are ``expected_headers``, ``body_equals``,
``body_contains``, ``json_equals``, ``json_contains`` and ``json_path_equals``
(paths such as ``$.items[0].state``).  Unknown options fail closed, so an agent
cannot accidentally produce an apparently successful but unexecuted assertion.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
import ipaddress
import json
import math
import re
import time
from typing import Any, Callable
from urllib.parse import unquote
import zlib

import httpx


_MAX_PAYLOAD_BYTES = 1024 * 1024
_MAX_DESCRIPTOR_BYTES = 1024 * 1024
_MAX_DESCRIPTOR_FILES = 128
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HOST_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_GRPC_PATH = re.compile(r"^/([A-Za-z_][A-Za-z0-9_.]*)/([A-Za-z_][A-Za-z0-9_]*)$")
_GRPC_CODES = (
    "OK", "CANCELLED", "UNKNOWN", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED",
    "NOT_FOUND", "ALREADY_EXISTS", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION", "ABORTED", "OUT_OF_RANGE", "UNIMPLEMENTED",
    "INTERNAL", "UNAVAILABLE", "DATA_LOSS", "UNAUTHENTICATED",
)
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_COMMON_FIELDS = {"protocol", "path", "service_name", "target_port", "headers", "body", "assertions"}
_HTTP_FIELDS = _COMMON_FIELDS | {"method", "query_params"}
_GRPC_FIELDS = _COMMON_FIELDS | {"method", "query_params", "body_base64", "descriptor_set_base64", "reflection"}
_ASSERTION_FIELDS = {"expected_headers", "body_equals", "body_contains", "json_equals", "json_contains", "json_path_equals"}
_FORBIDDEN_HEADERS = {
    "host", "connection", "content-length", "transfer-encoding", "upgrade",
    "proxy-authorization", "proxy-connection", "te", "trailer",
}


@dataclass(frozen=True)
class ProbeEndpoint:
    """An address already selected and ownership-verified by the controller."""

    service_name: str
    host: str
    port: int
    scheme: str = "http"
    target_port: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.service_name, str) or not self.service_name or len(self.service_name) > 256:
            raise ValueError("Probe endpoint service name is invalid")
        if not isinstance(self.host, str) or len(self.host) > 253:
            raise ValueError("Probe endpoint host is invalid")
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            if not _HOST_NAME.fullmatch(self.host):
                raise ValueError("Probe endpoint host is invalid") from None
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("Probe endpoint port is invalid")
        if not isinstance(self.scheme, str) or self.scheme not in {"http", "https", "grpc", "grpcs"}:
            raise ValueError("Probe endpoint scheme is invalid")
        if self.target_port is not None and (
            isinstance(self.target_port, bool) or not isinstance(self.target_port, int)
            or not 1 <= self.target_port <= 65535
        ):
            raise ValueError("Probe endpoint target port is invalid")

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def _invalid(message: str) -> ValueError:
    # Messages contain field names, never values supplied by an agent.
    return ValueError("Invalid probe payload: " + message)


def _validate_route(route: str, *, grpc: bool = False) -> None:
    if not isinstance(route, str) or not route.startswith("/") or len(route) > 4096:
        raise _invalid("require_route must be an absolute path, not a URL")
    decoded = route
    for _ in range(3):
        if (
            decoded.startswith("//") or "\\" in decoded or "?" in decoded or "#" in decoded
            or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
            or any(segment in {".", ".."} for segment in decoded.split("/"))
        ):
            raise _invalid("require_route contains an unsafe path")
        unquoted = unquote(decoded)
        if unquoted == decoded:
            break
        decoded = unquoted
    if grpc and not _GRPC_PATH.fullmatch(route):
        raise _invalid("gRPC require_route must be /package.Service/Method")


def _decode_base64(value: Any, field: str, limit: int) -> bytes:
    if not isinstance(value, str) or len(value) > (limit + 2) // 3 * 4:
        raise _invalid(field + " is invalid or exceeds the size limit")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise _invalid(field + " must be valid Base64") from None
    if len(decoded) > limit:
        raise _invalid(field + " exceeds the size limit")
    return decoded


def _validate_headers(value: Any, *, request: bool) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 100:
        raise _invalid("headers must be a bounded object")
    result = {}
    for name, content in value.items():
        if (
            not isinstance(name, str) or not _HEADER_NAME.fullmatch(name)
            or not isinstance(content, str) or len(content) > 8192
            or any(ord(character) < 32 or ord(character) >= 127 for character in content)
        ):
            raise _invalid("headers contain an invalid name or value")
        lower_name = name.lower()
        if request and (lower_name in _FORBIDDEN_HEADERS or lower_name.startswith(":") or lower_name.startswith("grpc-")):
            raise _invalid(f"header {lower_name} cannot override transport authority or framing")
        if lower_name in result:
            raise _invalid("headers contain a duplicate name")
        result[lower_name] = content
    return result


def _json_path_tokens(path: str) -> list[str | int]:
    if not isinstance(path, str) or not path or len(path) > 512:
        raise _invalid("json_path_equals contains an invalid path")
    remaining = path[1:] if path.startswith("$") else path
    if remaining.startswith("."):
        remaining = remaining[1:]
    elif path.startswith("$") and remaining and not remaining.startswith("["):
        raise _invalid("json_path_equals contains an invalid path")
    tokens: list[str | int] = []
    while remaining:
        if remaining.startswith("["):
            found = re.match(r"\[([0-9]+)\]", remaining)
            if not found:
                raise _invalid("json_path_equals contains an invalid path")
            tokens.append(int(found.group(1)))
        else:
            found = re.match(r"[A-Za-z0-9_-]+", remaining)
            if not found:
                raise _invalid("json_path_equals contains an invalid path")
            tokens.append(found.group(0))
        remaining = remaining[found.end():]
        if remaining.startswith("."):
            remaining = remaining[1:]
            if not remaining or remaining.startswith("["):
                raise _invalid("json_path_equals contains an invalid path")
        elif remaining and not remaining.startswith("["):
            raise _invalid("json_path_equals contains an invalid path")
    return tokens


def validate_probe_payload(payload: Mapping[str, Any], require_route: str | None = None) -> dict[str, Any]:
    """Normalize a complete agent request, or raise a value-free ``ValueError``.

    This function does no network I/O and can also gate static-agent output.
    ``require_route`` should always be supplied when validating a persisted check.
    """

    if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
        raise _invalid("request must be an object")
    try:
        encoded = json.dumps(dict(payload), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise _invalid("request exceeds the size limit")
        result = json.loads(encoded)
    except (TypeError, OverflowError, RecursionError, UnicodeError, ValueError):
        raise _invalid("request must be bounded JSON") from None
    protocol = result.get("protocol", "http")
    if not isinstance(protocol, str) or protocol not in {"http", "grpc"}:
        raise _invalid("protocol must be http or grpc")
    result["protocol"] = protocol
    allowed = _HTTP_FIELDS if protocol == "http" else _GRPC_FIELDS
    if set(result) - allowed:
        raise _invalid("request contains unsupported fields or an explicit destination")
    route = require_route if require_route is not None else result.get("path")
    if route is not None:
        _validate_route(route, grpc=protocol == "grpc")
    if "path" in result and result["path"] != route:
        raise _invalid("path must equal require_route")
    if "path" in result:
        _validate_route(result["path"], grpc=protocol == "grpc")
    if "service_name" in result and (
        not isinstance(result["service_name"], str) or not result["service_name"] or len(result["service_name"]) > 256
    ):
        raise _invalid("service_name is invalid")
    if "target_port" in result and (
        isinstance(result["target_port"], bool) or not isinstance(result["target_port"], int)
        or not 1 <= result["target_port"] <= 65535
    ):
        raise _invalid("target_port is invalid")
    result["headers"] = _validate_headers(result.get("headers", {}), request=True)
    assertions = result.get("assertions")
    expected_key = "expected_status_list" if protocol == "http" else "expected_grpc_status_list"
    if not isinstance(assertions, dict) or set(assertions) - (_ASSERTION_FIELDS | {expected_key}):
        raise _invalid("assertions contain unsupported fields")
    expected = assertions.get(expected_key)
    if not isinstance(expected, list) or not expected or len(expected) > 500:
        raise _invalid(expected_key + " must be a nonempty list")
    if protocol == "http":
        if any(isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599 for code in expected):
            raise _invalid("expected_status_list must contain HTTP status integers")
        if not {"method", "query_params", "body"}.issubset(result):
            raise _invalid("HTTP method, query_params and body are required")
        method = result["method"]
        if not isinstance(method, str) or method.upper() not in _HTTP_METHODS:
            raise _invalid("HTTP method is unsupported")
        result["method"] = method.upper()
        params = result["query_params"]
        if not isinstance(params, dict) or len(params) > 200:
            raise _invalid("query_params must be a bounded object")
        for key, content in params.items():
            values = content if isinstance(content, list) else [content]
            if not isinstance(key, str) or any(value is not None and not isinstance(value, (str, int, float, bool)) for value in values):
                raise _invalid("query_params contain unsupported values")
    else:
        if any(key.endswith("-bin") for key in result["headers"]):
            raise _invalid("binary gRPC request metadata is unsupported")
        normalized = []
        for code in expected:
            if isinstance(code, int) and not isinstance(code, bool) and 0 <= code < len(_GRPC_CODES):
                normalized.append(_GRPC_CODES[code])
            elif isinstance(code, str) and code.upper() in _GRPC_CODES:
                normalized.append(code.upper())
            else:
                raise _invalid("expected_grpc_status_list must contain gRPC code names or integers")
        assertions[expected_key] = normalized
        if result.get("method", "POST") != "POST" or result.get("query_params", {}) != {}:
            raise _invalid("gRPC does not support HTTP method overrides or query parameters")
        if "reflection" in result and not isinstance(result["reflection"], bool):
            raise _invalid("reflection must be a boolean")
        if "descriptor_set_base64" in result:
            descriptor_bytes = _decode_base64(result["descriptor_set_base64"], "descriptor_set_base64", _MAX_DESCRIPTOR_BYTES)
            pool = _descriptor_pool(descriptor_bytes)
            if route is not None:
                match = _GRPC_PATH.fullmatch(route)
                try:
                    method = pool.FindMethodByName(match.group(1) + "." + match.group(2))
                    if method.client_streaming or method.server_streaming:
                        raise ValueError
                except Exception:
                    raise _invalid("descriptor_set_base64 must define the required unary gRPC method") from None
        if "body_base64" in result:
            if result.get("body") is not None:
                raise _invalid("body and body_base64 are mutually exclusive")
            _decode_base64(result["body_base64"], "body_base64", _MAX_PAYLOAD_BYTES)
        elif "body" not in result:
            raise _invalid("gRPC body or body_base64 is required")
        elif not result.get("reflection", True) and "descriptor_set_base64" not in result:
            raise _invalid("gRPC JSON body requires descriptors or reflection")
    if "expected_headers" in assertions:
        assertions["expected_headers"] = _validate_headers(assertions["expected_headers"], request=False)
    if "body_equals" in assertions and not isinstance(assertions["body_equals"], str):
        raise _invalid("body_equals must be a string")
    if "body_contains" in assertions:
        contains = assertions["body_contains"]
        if isinstance(contains, str):
            assertions["body_contains"] = [contains]
        elif not isinstance(contains, list) or any(not isinstance(item, str) for item in contains):
            raise _invalid("body_contains must be a string or list of strings")
    if "json_path_equals" in assertions:
        paths = assertions["json_path_equals"]
        if not isinstance(paths, dict) or len(paths) > 100:
            raise _invalid("json_path_equals must be a bounded object")
        for path in paths:
            _json_path_tokens(path)
    return result


def _sensitive_header(name: str) -> bool:
    normalized = name.lower().replace("-", "").replace("_", "")
    return any(word in normalized for word in ("authorization", "cookie", "token", "password", "secret", "credential", "apikey"))


def _secrets(headers: Mapping[str, str]) -> set[str]:
    result = set()
    for key, value in headers.items():
        if not _sensitive_header(key) or not value:
            continue
        result.add(value)
        if key.lower() == "authorization" and " " in value:
            kind, credential = value.split(" ", 1)
            result.add(credential)
            if kind.lower() == "basic":
                try:
                    decoded = base64.b64decode(credential, validate=True).decode("utf-8")
                    result.add(decoded)
                    if ":" in decoded:
                        result.add(decoded.split(":", 1)[1])
                except (ValueError, UnicodeError, binascii.Error):
                    pass
        if "cookie" in key.lower():
            for item in value.split(";"):
                if "=" in item:
                    result.add(item.split("=", 1)[1].strip())
    return {value for value in result if value}


def _redact_text(value: str, secrets: set[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if value == secret:
            return "[redacted]"
        if len(secret) >= 4:
            value = value.replace(secret, "[redacted]")
    return value


def _redact_json(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, dict):
        return {key: "[redacted]" if _sensitive_header(key) else _redact_json(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item, secrets) for item in value]
    return _redact_text(value, secrets) if isinstance(value, str) else value


def _redact_bytes(value: bytes, secrets: set[str]) -> bytes:
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            value = value.replace(secret.encode("utf-8"), b"[redacted]")
        elif value == secret.encode("utf-8"):
            return b"[redacted]"
    return value


def _headers_for_result(headers: Mapping[str, str], secrets: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    remaining = 16384
    for name, content in headers.items():
        if _sensitive_header(name):
            continue
        value = _redact_text(str(content), secrets)[:max(0, remaining)]
        remaining -= len(name) + len(value)
        if remaining < 0:
            break
        result[name] = value
    return result


def _json_contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _json_contains(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) >= len(expected) and all(_json_contains(value, expected[index]) for index, value in enumerate(actual[:len(expected)]))
    return _json_scalar_equal(actual, expected)


def _json_scalar_equal(actual: Any, expected: Any) -> bool:
    # JSON numbers have one category. Python's bool is an int subclass, but a
    # JSON boolean must never satisfy a numeric assertion (or vice versa).
    if type(actual) in {int, float} and type(expected) in {int, float}:
        return actual == expected
    return type(actual) is type(expected) and actual == expected


def _json_equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and set(actual) == set(expected) and all(_json_equal(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(_json_equal(value, expected[index]) for index, value in enumerate(actual))
    return _json_scalar_equal(actual, expected)


def _assert_response(assertions: dict[str, Any], body: str, decoded_json: Any, *, json_available: bool, headers: Mapping[str, str]) -> list[str]:
    errors = []
    if "expected_headers" in assertions and any(headers.get(name) != value for name, value in assertions["expected_headers"].items()):
        errors.append("Response header assertion failed")
    if "body_equals" in assertions and body != assertions["body_equals"]:
        errors.append("Response body equality assertion failed")
    if "body_contains" in assertions and any(item not in body for item in assertions["body_contains"]):
        errors.append("Response body content assertion failed")
    if "json_equals" in assertions and (not json_available or not _json_equal(decoded_json, assertions["json_equals"])):
        errors.append("Response JSON equality assertion failed")
    if "json_contains" in assertions and (not json_available or not _json_contains(decoded_json, assertions["json_contains"])):
        errors.append("Response JSON subset assertion failed")
    for path, expected in assertions.get("json_path_equals", {}).items():
        actual = decoded_json
        try:
            if not json_available:
                raise KeyError(path)
            for token in _json_path_tokens(path):
                if isinstance(token, int) and not isinstance(actual, list):
                    raise KeyError(path)
                if isinstance(token, str) and not isinstance(actual, dict):
                    raise KeyError(path)
                actual = actual[token]
            if not _json_equal(actual, expected):
                raise KeyError(path)
        except (KeyError, IndexError, TypeError):
            errors.append("Response JSON path assertion failed")
    return errors


def _descriptor_pool(serialized_set: bytes) -> Any:
    try:
        from google.protobuf import descriptor_pb2, descriptor_pool

        descriptors = descriptor_pb2.FileDescriptorSet.FromString(serialized_set)
        if not descriptors.file or len(descriptors.file) > _MAX_DESCRIPTOR_FILES:
            raise ValueError
        pending = {item.name: item for item in descriptors.file}
        if len(pending) != len(descriptors.file):
            raise ValueError
        pool = descriptor_pool.DescriptorPool()
        while pending:
            progressed = False
            for name, descriptor in list(pending.items()):
                if any(dependency in pending for dependency in descriptor.dependency):
                    continue
                pool.AddSerializedFile(descriptor.SerializeToString())
                del pending[name]
                progressed = True
            if not progressed:
                raise ValueError
        return pool
    except Exception:
        raise _invalid("descriptor_set_base64 must contain complete valid Protobuf descriptors") from None


class _ProbeFailure(Exception):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message


def _decode_http_content(data: bytes, content_encoding: str, limit: int) -> tuple[bytes, bool]:
    """Decode common encodings without allowing a compressed response bomb.

    HTTPX's automatic streaming decoder can allocate the entire expansion of
    one compressed network chunk before a caller's size check.  Read raw chunks
    instead and use zlib's explicit output bound.  Identity is requested by
    default; unsupported encodings fail closed rather than remove this bound.
    """

    encodings = [item.strip().lower() for item in content_encoding.split(",") if item.strip()]
    if len(encodings) > 2:
        raise _ProbeFailure("invalid_response", "HTTP response used unsupported content encoding")
    for encoding in reversed(encodings):
        if encoding in {"identity", ""}:
            continue
        if encoding not in {"gzip", "x-gzip", "deflate"}:
            raise _ProbeFailure("invalid_response", "HTTP response used unsupported content encoding")
        windows = (16 + zlib.MAX_WBITS,) if encoding in {"gzip", "x-gzip"} else (zlib.MAX_WBITS, -zlib.MAX_WBITS)
        decoded = None
        for window in windows:
            try:
                decoder = zlib.decompressobj(window)
                decoded = decoder.decompress(data, limit + 1)
                break
            except zlib.error:
                continue
        if decoded is None:
            raise _ProbeFailure("invalid_response", "HTTP response contained invalid compressed data")
        if len(decoded) > limit or decoder.unconsumed_tail:
            return decoded[:limit], True
        if not decoder.eof or decoder.unused_data:
            raise _ProbeFailure("invalid_response", "HTTP response contained incomplete compressed data")
        data = decoded
    return data, False


class ProbeClient:
    def __init__(
        self,
        timeout_seconds: float = 30,
        max_response_bytes: int = 65536,
        *,
        transport: httpx.BaseTransport | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
        grpc_channel_factory: Callable[[ProbeEndpoint, tuple[tuple[str, Any], ...]], Any] | None = None,
    ) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Probe timeout must be positive and finite")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes <= 0:
            raise ValueError("Probe response limit must be a positive integer")
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes
        self.transport = transport
        self.client_factory = client_factory
        self.grpc_channel_factory = grpc_channel_factory

    def run(self, payload: Mapping[str, Any], *, require_route: str, endpoints: list[ProbeEndpoint]) -> dict[str, Any]:
        try:
            _validate_route(require_route)
            normalized = validate_probe_payload(payload, require_route)
            if not isinstance(endpoints, (list, tuple)) or any(not isinstance(endpoint, ProbeEndpoint) for endpoint in endpoints):
                raise _invalid("deployment endpoints are invalid")
        except (ValueError, TypeError, RecursionError):
            return {"passed": False, "error_type": "invalid_payload", "error": "Probe request validation failed", "attempts": []}
        candidates = [
            endpoint for endpoint in endpoints
            if ("service_name" not in normalized or endpoint.service_name == normalized["service_name"])
            and ("target_port" not in normalized or endpoint.target_port == normalized["target_port"])
            and (normalized["protocol"] == "grpc" or endpoint.scheme in {"http", "https"})
        ]
        if not candidates:
            return {"passed": False, "protocol": normalized["protocol"], "error_type": "no_endpoint", "error": "No matching deployment-owned probe endpoint is available", "attempts": []}
        attempts = []
        for endpoint in candidates:
            result = {
                "protocol": normalized["protocol"], "service_name": endpoint.service_name,
                "target_port": endpoint.target_port, "path": require_route,
            }
            try:
                execute = self._run_http if normalized["protocol"] == "http" else self._run_grpc
                result.update(execute(normalized, require_route, endpoint))
            except _ProbeFailure as exc:
                result.update(passed=False, error_type=exc.kind, error=exc.message)
            except Exception:
                # Transport exceptions may contain URLs, credentials or user data.
                result.update(passed=False, error_type="transport_error", error="Probe transport could not complete the request")
            attempts.append(result)
            if result["passed"]:
                break
        return {**attempts[-1], "attempts": attempts}

    def _run_http(self, payload: dict[str, Any], route: str, endpoint: ProbeEndpoint) -> dict[str, Any]:
        headers = {"accept-encoding": "identity", **payload["headers"]}
        secrets = _secrets(headers)
        request = {"params": payload["query_params"], "headers": headers}
        if payload["body"] is not None:
            if isinstance(payload["body"], str):
                request["content"] = payload["body"].encode("utf-8")
            else:
                request["json"] = payload["body"]
        body = bytearray()
        oversized = False
        deadline = time.monotonic() + self.timeout_seconds
        options = {"timeout": httpx.Timeout(self.timeout_seconds), "follow_redirects": False, "trust_env": False}
        if self.transport is not None:
            options["transport"] = self.transport
        try:
            with self.client_factory(**options) as client:
                with client.stream(payload["method"], f"{endpoint.scheme}://{endpoint.authority}{route}", **request) as response:
                    # A testing transport may supply a pre-read Response. Such
                    # content has already been decoded by HTTPX.
                    content_encoding = "" if response.is_stream_consumed else response.headers.get("content-encoding", "")
                    chunks = iter([response.content]) if response.is_stream_consumed else response.iter_raw()
                    for chunk in chunks:
                        if time.monotonic() > deadline:
                            raise _ProbeFailure("timeout", "HTTP probe exceeded its configured timeout")
                        remaining = self.max_response_bytes - len(body)
                        body.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            oversized = True
                            break
                    if not oversized:
                        decoded, oversized = _decode_http_content(bytes(body), content_encoding, self.max_response_bytes)
                        body = bytearray(decoded)
                    encoding = response.encoding or "utf-8"
                    try:
                        text = bytes(body).decode(encoding, errors="replace")
                    except LookupError:
                        text = bytes(body).decode("utf-8", errors="replace")
                    response_headers = dict(response.headers)
                    status = response.status_code
        except httpx.TimeoutException:
            raise _ProbeFailure("timeout", "HTTP probe exceeded its configured timeout") from None
        except httpx.HTTPError:
            raise _ProbeFailure("transport_error", "HTTP probe could not complete the request") from None
        decoded_json = None
        json_available = False
        if not oversized:
            try:
                decoded_json = json.loads(text)
                # Python accepts NaN/Infinity and numeric overflow in JSON by
                # default. Such results cannot be stored/returned as JSON and
                # must not satisfy a purported structured response assertion.
                json.dumps(decoded_json, ensure_ascii=False, allow_nan=False).encode("utf-8")
                json_available = True
            except (ValueError, RecursionError, UnicodeError):
                pass
        errors = _assert_response(payload["assertions"], text, decoded_json, json_available=json_available, headers=response_headers)
        if status not in payload["assertions"]["expected_status_list"]:
            errors.insert(0, "HTTP response status assertion failed")
        result: dict[str, Any] = {
            "passed": not errors and not oversized,
            "response": {
                "status_code": status, "headers": _headers_for_result(response_headers, secrets),
                "body": _redact_text(text, secrets), "body_bytes": len(body), "truncated": oversized,
            },
            "assertion_errors": errors,
        }
        if json_available:
            result["response"]["json"] = _redact_json(decoded_json, secrets)
        if oversized:
            result.update(error_type="response_too_large", error="HTTP response exceeded the configured size limit")
        elif errors:
            result.update(error_type="assertion_failed", error="HTTP response did not satisfy probe assertions")
        return result

    def _run_grpc(self, payload: dict[str, Any], route: str, endpoint: ProbeEndpoint) -> dict[str, Any]:
        try:
            import grpc
            from google.protobuf import json_format, message_factory
        except ImportError:
            raise _ProbeFailure("dependencies_unavailable", "gRPC probe dependencies are unavailable") from None
        options = (
            ("grpc.enable_http_proxy", 0),
            ("grpc.max_receive_message_length", self.max_response_bytes + 1),
            ("grpc.max_send_message_length", _MAX_PAYLOAD_BYTES),
        )
        if self.grpc_channel_factory is not None:
            channel = self.grpc_channel_factory(endpoint, options)
        elif endpoint.scheme in {"https", "grpcs"}:
            channel = grpc.secure_channel(endpoint.authority, grpc.ssl_channel_credentials(), options=options)
        else:
            channel = grpc.insecure_channel(endpoint.authority, options=options)
        metadata = list(payload["headers"].items())
        if any(key.endswith("-bin") for key, _ in metadata):
            channel.close()
            raise _ProbeFailure("invalid_payload", "Binary gRPC request metadata is unsupported")
        secrets = _secrets(payload["headers"])
        response_type = None
        deadline = time.monotonic() + self.timeout_seconds
        with channel:
            if "body_base64" in payload:
                request = _decode_base64(payload["body_base64"], "body_base64", _MAX_PAYLOAD_BYTES)
                pool = _descriptor_pool(_decode_base64(payload["descriptor_set_base64"], "descriptor_set_base64", _MAX_DESCRIPTOR_BYTES)) if "descriptor_set_base64" in payload else None
            else:
                pool = _descriptor_pool(_decode_base64(payload["descriptor_set_base64"], "descriptor_set_base64", _MAX_DESCRIPTOR_BYTES)) if "descriptor_set_base64" in payload else self._reflection_pool(channel, route, metadata, deadline)
            if pool is not None:
                match = _GRPC_PATH.fullmatch(route)
                try:
                    method = pool.FindMethodByName(match.group(1) + "." + match.group(2))
                    if method.client_streaming or method.server_streaming:
                        raise ValueError
                    request_type = message_factory.GetMessageClass(method.input_type)
                    response_type = message_factory.GetMessageClass(method.output_type)
                    if "body_base64" not in payload:
                        request = json_format.ParseDict(payload["body"] if payload["body"] is not None else {}, request_type()).SerializeToString()
                except Exception:
                    raise _ProbeFailure("invalid_payload", "gRPC route or JSON body does not match a supported unary Protobuf method") from None
            if len(request) > _MAX_PAYLOAD_BYTES:
                raise _ProbeFailure("invalid_payload", "gRPC request exceeded the configured size limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ProbeFailure("timeout", "gRPC probe exceeded its configured timeout")
            response_data = b""
            call = None
            try:
                response_data, call = channel.unary_unary(route).with_call(request, timeout=remaining, metadata=metadata)
                status = "OK"
                status_code = 0
                details = ""
            except grpc.RpcError as exc:
                status = exc.code().name
                status_code = exc.code().value[0]
                details = _redact_text(exc.details() or "", secrets)[:4096]
                call = exc
            response_headers: dict[str, str] = {}
            for read_metadata in (getattr(call, "initial_metadata", None), getattr(call, "trailing_metadata", None)):
                if read_metadata is None:
                    continue
                try:
                    for key, value in read_metadata() or ():
                        response_headers[key] = base64.b64encode(value).decode("ascii") if isinstance(value, bytes) else str(value)
                except Exception:
                    pass
        oversized = len(response_data) > self.max_response_bytes
        if status == "RESOURCE_EXHAUSTED" and "larger than max" in details.lower():
            oversized = True
        response_data = response_data[:self.max_response_bytes]
        decoded_json = None
        json_available = False
        if response_type is not None and not oversized and status == "OK":
            try:
                decoded_json = json_format.MessageToDict(response_type.FromString(response_data), preserving_proto_field_name=True)
                json_available = True
            except Exception:
                raise _ProbeFailure("invalid_response", "gRPC response was not valid for the declared Protobuf output type") from None
        text = json.dumps(decoded_json, ensure_ascii=False, sort_keys=True) if json_available else response_data.decode("utf-8", errors="replace")
        errors = _assert_response(payload["assertions"], text, decoded_json, json_available=json_available, headers=response_headers)
        if status not in payload["assertions"]["expected_grpc_status_list"]:
            errors.insert(0, "gRPC response status assertion failed")
        result = {
            "passed": not errors and not oversized, "assertion_errors": errors,
            "response": {
                "grpc_status": status, "grpc_status_code": status_code,
                "details": details, "headers": _headers_for_result(response_headers, secrets),
                "body_base64": base64.b64encode(_redact_bytes(response_data, secrets)).decode("ascii"),
                "body_bytes": len(response_data), "truncated": oversized,
            },
        }
        if json_available:
            result["response"]["json"] = _redact_json(decoded_json, secrets)
        if oversized:
            result.update(error_type="response_too_large", error="gRPC response exceeded the configured size limit")
        elif status == "DEADLINE_EXCEEDED":
            # An explicitly asserted DEADLINE_EXCEEDED remains a successful test.
            if errors:
                result.update(error_type="timeout", error="gRPC probe exceeded its configured timeout")
        elif errors:
            result.update(error_type="assertion_failed", error="gRPC response did not satisfy probe assertions")
        return result

    def _reflection_pool(self, channel: Any, route: str, metadata: list[tuple[str, str]], deadline: float) -> Any:
        try:
            from google.protobuf import descriptor_pb2
            from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc
        except ImportError:
            raise _ProbeFailure("dependencies_unavailable", "gRPC reflection dependencies are unavailable") from None
        stub = reflection_pb2_grpc.ServerReflectionStub(channel)
        files: dict[str, Any] = {}
        total_bytes = 0

        def fetch(*, symbol: str | None = None, filename: str | None = None) -> None:
            nonlocal total_bytes
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ProbeFailure("timeout", "gRPC reflection exceeded its configured timeout")
            request = reflection_pb2.ServerReflectionRequest(file_containing_symbol=symbol) if symbol is not None else reflection_pb2.ServerReflectionRequest(file_by_filename=filename)
            stream = stub.ServerReflectionInfo(iter([request]), timeout=remaining, metadata=metadata)
            try:
                response = next(stream)
                if response.WhichOneof("message_response") != "file_descriptor_response":
                    raise ValueError
                for serialized in response.file_descriptor_response.file_descriptor_proto:
                    total_bytes += len(serialized)
                    if total_bytes > _MAX_DESCRIPTOR_BYTES:
                        raise ValueError
                    descriptor = descriptor_pb2.FileDescriptorProto.FromString(serialized)
                    files[descriptor.name] = descriptor
                    if len(files) > _MAX_DESCRIPTOR_FILES:
                        raise ValueError
            except _ProbeFailure:
                raise
            except Exception:
                raise _ProbeFailure("reflection_failed", "gRPC reflection could not resolve the required route") from None
            finally:
                stream.cancel()

        fetch(symbol=_GRPC_PATH.fullmatch(route).group(1))
        queried = set()
        while True:
            missing = {name for descriptor in files.values() for name in descriptor.dependency if name not in files}
            if not missing:
                break
            if missing & queried:
                raise _ProbeFailure("reflection_failed", "gRPC reflection returned incomplete descriptors")
            for name in missing:
                queried.add(name)
                fetch(filename=name)
        descriptors = descriptor_pb2.FileDescriptorSet()
        descriptors.file.extend(files.values())
        try:
            return _descriptor_pool(descriptors.SerializeToString())
        except ValueError:
            raise _ProbeFailure("reflection_failed", "gRPC reflection returned invalid descriptors") from None
