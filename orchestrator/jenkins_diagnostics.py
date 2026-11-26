"""Bounded, redacted diagnostics derived from Jenkins console evidence."""

from __future__ import annotations

import json
import re
from base64 import b64encode
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote


# Documented limits for APPS-visible failure payloads.
# Summary string (build.error / alerts.message) default ceiling.
DEFAULT_SUMMARY_MAX_CHARS = 4000
# Nearby console excerpt stored on build.raw_log_excerpt (ANSI-stripped; secrets not scrubbed).
DEFAULT_EXCERPT_MAX_CHARS = 32000
# How many console lines around the primary error to keep in the excerpt.
DEFAULT_EXCERPT_CONTEXT_BEFORE = 20
DEFAULT_EXCERPT_CONTEXT_AFTER = 40

# Stable error contract codes for APPS (error_detail.code).
CODE_COMPOSE_CONFIG_ERROR = "COMPOSE_CONFIG_ERROR"
CODE_JENKINS_BUILD_ERROR = "JENKINS_BUILD_ERROR"

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONSOLE_NOTE = re.compile(r"\x1b\[8mha:.*?\x1b\[0m", re.DOTALL)
_TIMESTAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2}T[^\]]+\]\s*")
_STAGE = re.compile(r"\[Pipeline\]\s*\{\s*\((.+)\)\s*$")
_EXCEPTION_HOOK_START = re.compile(r"^Error in sys\.excepthook:")
_EXCEPTION_HOOK_END = re.compile(r"^Original exception was:")
_INFORMATIONAL_GIT = re.compile(r"^(?:HEAD is now at\b|Checking out Revision\b)", re.IGNORECASE)
_WRAPPER = re.compile(
    r"script returned exit code|did not complete successfully|failed to solve|executor failed|"
    r"build step .+ marked build as failure|hudson\.AbortException|subprocess\.CalledProcessError",
    re.IGNORECASE,
)
_ERROR = re.compile(
    r"\b(?:error|fatal|exception|failed|failure|traceback|timed out|timeout|aborted)\b|"
    r"\berror TS\d+|\b[\w.]*(?:Error|Exception):|\b(?:ENOENT|ENOTFOUND|EAI_AGAIN|EBADENGINE|EACCES|ENOSPC)\b|"
    r"not found|no such file|not permitted|permission denied|uses env_file|\bmust\b|is required|"
    r"\binvalid\b|\bunsupported\b|\bunsafe\b|does not exist|already exists|exceeds .+ limit|could not be verified",
    re.IGNORECASE,
)
# (cause_zh, subcode, code, pattern, remediation_zh)
_CAUSE_SPECS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "包管理器注册表请求失败",
        "PACKAGE_REGISTRY_REQUEST_FAILED",
        CODE_JENKINS_BUILD_ERROR,
        r"error when performing the request to https?://registry\.",
        "请检查构建环境能否访问 npm/pip 等包注册表，以及是否需要配置代理或镜像源。",
    ),
    (
        "DNS 解析失败",
        "DNS_RESOLUTION_FAILED",
        CODE_JENKINS_BUILD_ERROR,
        r"could not resolve host|couldn't resolve host|name or service not known|temporary failure in name resolution|\bENOTFOUND\b|\bEAI_AGAIN\b",
        "请核对仓库/依赖域名是否正确，并确认构建节点 DNS 与出网正常。",
    ),
    (
        "TLS 证书校验失败",
        "TLS_CERTIFICATE_FAILED",
        CODE_JENKINS_BUILD_ERROR,
        r"\bx509:|SSL certificate problem|certificate verify failed|certificate signed by unknown authority|unable to (?:get local issuer|verify the first) certificate",
        "请检查目标站点证书链是否完整，或构建节点是否信任对应 CA。",
    ),
    (
        "依赖运行时版本不兼容",
        "RUNTIME_ENGINE_INCOMPATIBLE",
        CODE_JENKINS_BUILD_ERROR,
        r"\bEBADENGINE\b|unsupported engine|not compatible with your version of node",
        "请按 package.json engines（或等价约束）调整 Node/运行时版本，或放宽 engines 声明。",
    ),
    (
        "连接失败",
        "CONNECTION_FAILED",
        CODE_JENKINS_BUILD_ERROR,
        r"connection refused|connection timed out|connect ETIMEDOUT|network is unreachable|no route to host",
        "请检查镜像仓库/依赖服务地址、端口与构建节点网络连通性。",
    ),
    (
        "认证或权限失败",
        "AUTH_OR_PERMISSION_FAILED",
        CODE_JENKINS_BUILD_ERROR,
        r"authentication failed|unauthorized|access denied|permission denied|requested access.+denied|denied:|status(?: code)?[: =]+(?:401|403)\b",
        "请检查仓库/镜像仓库凭据是否有效，以及账号是否具备拉取或推送权限。",
    ),
    (
        "构建上下文缺失",
        "BUILD_CONTEXT_MISSING",
        CODE_COMPOSE_CONFIG_ERROR,
        r"build context does not exist|service \S+ build context does not exist",
        "请检查 compose 中该服务的 build.context 是否指向源码仓库或 workspace 内真实存在的目录；例如 context 为 ./backend 时，源码根目录下必须有 backend/。",
    ),
    (
        "Dockerfile 缺失",
        "DOCKERFILE_MISSING",
        CODE_COMPOSE_CONFIG_ERROR,
        r"(?:dockerfile|Dockerfile).+(?:not found|does not exist)|service \S+ Dockerfile does not exist",
        "请确认 build.dockerfile 指向的文件相对 build.context 真实存在，文件名大小写与路径一致。",
    ),
    (
        "工作区路径缺失",
        "WORKSPACE_PATH_MISSING",
        CODE_COMPOSE_CONFIG_ERROR,
        r"workspace .+ does not exist",
        "请确认 workspace 上传内容完整，且 compose / 挂载路径指向已上传的相对路径。",
    ),
    (
        "文件或路径缺失",
        "FILE_OR_PATH_MISSING",
        CODE_JENKINS_BUILD_ERROR,
        r"no such file|\bENOENT\b|cannot find module|(?:package\.json).+(?:not found|does not exist)",
        "请检查报错路径对应的源文件、依赖模块或配置是否已提交到仓库/workspace。",
    ),
    (
        "存储空间不足",
        "DISK_SPACE_EXHAUSTED",
        CODE_JENKINS_BUILD_ERROR,
        r"no space left on device|\bENOSPC\b",
        "构建节点磁盘已满，请联系平台清理构建缓存或扩容后再重试。",
    ),
    (
        "端口冲突",
        "PORT_CONFLICT",
        CODE_JENKINS_BUILD_ERROR,
        r"address already in use|port is already allocated",
        "请更换服务端口映射，或释放占用该端口的进程/服务后重试。",
    ),
)

_CAUSES = tuple((cause, pattern) for cause, _subcode, _code, pattern, _remediation in _CAUSE_SPECS)
_CAUSE_BY_LABEL = {
    cause: {"subcode": subcode, "code": code, "remediation": remediation}
    for cause, subcode, code, _pattern, remediation in _CAUSE_SPECS
}


@dataclass(frozen=True)
class FailureDiagnostic:
    """Structured failure-site evidence for API consumers (APPS / conductor)."""

    summary: str
    stage: str = ""
    node: str = ""
    location: str = ""
    source_file: str = ""
    cause: str = ""
    reason: str = ""
    context: list[str] = field(default_factory=list)
    exit_code: str = ""
    console_line: int | None = None
    console_byte_start: int = 0
    raw_log_excerpt: str = ""
    recognized: bool = False
    code: str = ""
    subcode: str = ""
    remediation: str = ""
    service: str = ""
    config_path: str = ""

    def to_error_detail(
        self,
        *,
        build_id: str | None = None,
        jenkins_build_number: int | None = None,
        jenkins_job: str | None = None,
        result: str | None = None,
    ) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "code": self.code or (CODE_JENKINS_BUILD_ERROR if self.recognized or self.raw_log_excerpt else None),
            "subcode": self.subcode or ("UNKNOWN" if not self.recognized else None),
            "stage": self.stage or None,
            "node": self.node or None,
            "location": self.location or None,
            "source_file": self.source_file or None,
            "cause": self.cause or None,
            "reason": self.reason or None,
            "context": list(self.context),
            "exit_code": self.exit_code or None,
            "console_line": self.console_line,
            "console_byte_start": self.console_byte_start or None,
            "recognized": self.recognized,
            "summary": self.summary,
            "remediation": self.remediation or None,
            "service": self.service or None,
            "config_path": self.config_path or None,
        }
        if build_id:
            detail["build_id"] = build_id
        if jenkins_build_number is not None:
            detail["jenkins_build_number"] = jenkins_build_number
        if jenkins_job:
            detail["jenkins_job"] = jenkins_job
        if result:
            detail["jenkins_result"] = result
        return {key: value for key, value in detail.items() if value is not None and value != []}


def _classify_cause(line: str) -> tuple[str, str, str, str]:
    """Return (cause_zh, subcode, code, remediation_zh) for one console line."""

    for cause, subcode, code, pattern, remediation in _CAUSE_SPECS:
        if re.search(pattern, line, re.IGNORECASE):
            return cause, subcode, code, remediation
    return "", "", "", ""


def _extract_service_name(reason: str) -> str:
    match = re.search(r"service\s+(\S+)\s+build context does not exist", reason, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"service\s+(\S+)\s+Dockerfile does not exist", reason, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def _extract_config_path(service: str, subcode: str) -> str:
    if not service:
        return ""
    if subcode == "BUILD_CONTEXT_MISSING":
        return f"services.{service}.build.context"
    if subcode == "DOCKERFILE_MISSING":
        return f"services.{service}.build.dockerfile"
    return f"services.{service}.build"


def redact_console(text: str, sensitive_values: Iterable[str] = ()) -> str:
    text = _CONSOLE_NOTE.sub("", text)
    text = _ANSI.sub("", text)
    variants = set()
    for value in sensitive_values:
        if not isinstance(value, str) or not value:
            continue
        variants.update((
            value, quote(value, safe=""), b64encode(value.encode()).decode(),
            json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1],
        ))
    for value in sorted(variants, key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, "***" + "\n" * value.count("\n"))
        else:
            text = re.sub(r"(?<!\w)" + re.escape(value) + r"(?!\w)", "***", text)
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
        lambda match: "***" + "\n" * match.group(0).count("\n"), text, flags=re.DOTALL,
    )
    text = re.sub(r"(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://)[^\s/@]+@", r"\1***@", text, flags=re.IGNORECASE)
    text = re.sub(r"(https?://[^\s?'\"<>]+)\?[^\s'\"<>]+", r"\1?***", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(Bearer|Basic)\s+[a-z0-9+/_.=~-]+", r"\1 ***", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?<![\w.-])([\"']?[\w.-]*(?:password|passwd|token|secret|api[-_]?key|authorization|credential)[\w.-]*[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)",
        r"\1***",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)


def _shorten(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _excerpt_block(lines: list[str], index: int, *, before: int, after: int, max_chars: int) -> str:
    start = max(0, index - before)
    end = min(len(lines), index + after + 1)
    numbered = []
    for offset, line in enumerate(lines[start:end], start=start + 1):
        marker = ">>" if offset == index + 1 else "  "
        numbered.append(f"{marker} L{offset}: {line}")
    text = "\n".join(numbered)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _strip_console_noise(text: str) -> str:
    """Remove terminal noise without secret redaction (for cause/location matching)."""
    text = _CONSOLE_NOTE.sub("", text)
    text = _ANSI.sub("", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)


def diagnose_failure(
    console: str,
    *,
    start: int = 0,
    sensitive_values: Iterable[str] = (),
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    excerpt_max_chars: int = DEFAULT_EXCERPT_MAX_CHARS,
) -> FailureDiagnostic:
    """Parse console text into a summary string plus structured failure-site fields.

    ``raw_log_excerpt`` keeps ANSI-stripped console text for APPS diagnosis and does
    not scrub application secrets (apps own their compose/env). ``summary`` /
    ``reason`` still redact known secrets for alerts and short UI banners.
    """

    # Classify on noise-stripped text so auth/token: connection refused keeps cause.
    cleaned = _strip_console_noise(console)
    redacted = redact_console(console, sensitive_values)
    clean_lines = [_TIMESTAMP.sub("", line).strip() for line in cleaned.splitlines()]
    redacted_lines = [_TIMESTAMP.sub("", line).strip() for line in redacted.splitlines()]
    if len(clean_lines) != len(redacted_lines):
        clean_lines = list(redacted_lines)
    exception_hook_lines = set()
    in_exception_hook = False
    for index, line in enumerate(clean_lines):
        if _EXCEPTION_HOOK_START.search(line):
            in_exception_hook = True
        if in_exception_hook and not _EXCEPTION_HOOK_END.search(line):
            exception_hook_lines.add(index)
        elif in_exception_hook:
            in_exception_hook = False
    stages: list[str] = []
    node = ""
    candidates = []
    exit_code = ""
    for index, line in enumerate(clean_lines):
        if index in exception_hook_lines:
            continue
        stage = _STAGE.search(line)
        if stage:
            stages.append(stage.group(1))
            continue
        if re.search(r"\[Pipeline\]\s*// stage\s*$", line):
            if stages:
                stages.pop()
            continue
        running = re.search(r"Running on (\S+) in ", line)
        if running:
            node = running.group(1)
        code_match = re.search(r"(?:script returned exit code|exit code[: ]+)\s*(\d+)", line, re.IGNORECASE)
        if code_match and not exit_code:
            exit_code = code_match.group(1)
        if not line or _INFORMATIONAL_GIT.search(line) or line.startswith(("[Pipeline]", "+ ", "Finished:", 'Stage "')):
            continue
        if line.startswith("Timeout set to expire in "):
            continue
        if re.search(r"\bnpm warn\b|^warning:|\b0 (?:errors|failures|failed)\b", line, re.IGNORECASE):
            continue
        cause, subcode, err_code, remediation = _classify_cause(line)
        if "skipped due to" in line.lower() or not (cause or _ERROR.search(line)):
            continue
        if cause:
            priority = 100
        elif _WRAPPER.search(line):
            priority = 10
        elif line.startswith("Traceback "):
            priority = 20
        else:
            priority = 50
        candidates.append((priority, index, " / ".join(stages), node, cause, subcode, err_code, remediation))
    if not candidates:
        fallback = "日志中未识别出具体错误；请结合 raw_log_excerpt 与 Jenkins 控制台排查"
        excerpt = _excerpt_block(
            clean_lines,
            max(0, len(clean_lines) - 1),
            before=DEFAULT_EXCERPT_CONTEXT_BEFORE,
            after=0,
            max_chars=excerpt_max_chars,
        ) if clean_lines else ""
        return FailureDiagnostic(
            summary=fallback,
            raw_log_excerpt=excerpt,
            console_byte_start=start,
            recognized=False,
            code=CODE_JENKINS_BUILD_ERROR,
            subcode="UNKNOWN",
            remediation="请优先阅读 raw_log_excerpt；若仍无法定位，打开对应 Jenkins 构建控制台查看完整日志。",
        )
    primary = [candidate for candidate in candidates if "Declarative: Post Actions" not in candidate[2]]
    if primary:
        candidates = primary
    _, index, stage, node, cause, subcode, err_code, remediation = max(candidates, key=lambda candidate: candidate[0])
    if cause and not subcode:
        mapped = _CAUSE_BY_LABEL.get(cause, {})
        subcode = mapped.get("subcode", "")
        err_code = mapped.get("code", "")
        remediation = mapped.get("remediation", "")
    if not cause:
        err_code = CODE_JENKINS_BUILD_ERROR
        subcode = "UNCLASSIFIED_CONSOLE_ERROR"
        remediation = "错误已定位到控制台行但尚未映射稳定子码；请根据 raw_log_excerpt 中的原因行修复后重试。"
    parts = []
    if stage:
        parts.append("阶段=" + _shorten(stage, 120))
    if node:
        parts.append("节点=" + _shorten(node, 60))
    location = f"console L{index + 1}"
    if start:
        location = f"console 尾部片段 L{index + 1}（片段内行号，字节起点 {start}）"
    nearby = clean_lines[max(0, index - 8) : index + 15]
    source = ""
    for line in nearby:
        match = re.search(r"(?<![\w./-])[\w./-]*Dockerfile[\w.-]*:\d+", line)
        if match:
            source = match.group(0)
            break
    if not source:
        for line in reversed(clean_lines[max(0, index - 8) : index + 1]):
            match = re.search(r'File "([^"]+)", line (\d+)', line)
            if match:
                source = f"{match.group(1)}:{match.group(2)}"
                break
    parts.append("位置=" + (_shorten(source, 120) + "，" if source else "") + location)
    raw_reason_line = clean_lines[index]
    reason_line = redact_console(raw_reason_line, sensitive_values).strip() or raw_reason_line
    reason = _shorten(reason_line, 480)
    parts.append("原因=" + (cause + "：" if cause else "") + reason)
    context: list[str] = []
    for line in clean_lines[index + 1 : index + 9]:
        if not line or line.startswith("[Pipeline]") or line.startswith("Stage ") or line.startswith("Finished:"):
            break
        if line == clean_lines[index] or _WRAPPER.search(line):
            continue
        context.append(_shorten(redact_console(line, sensitive_values).strip() or line, 160))
    requirements = [line for line in context if re.search(r"\b(?:required|actual):", line, re.IGNORECASE)]
    context = (requirements or context)[:2]
    if context:
        parts.append("上下文=" + " / ".join(context))
    if exit_code:
        parts.append("退出码=" + exit_code)
    if remediation:
        parts.append("建议=" + _shorten(remediation, 240))
    summary = _shorten("；".join(parts), summary_max_chars)
    # Prefer unredacted (noise-stripped) excerpt so APPS can see the real failure text.
    excerpt = _excerpt_block(
        clean_lines,
        index,
        before=DEFAULT_EXCERPT_CONTEXT_BEFORE,
        after=DEFAULT_EXCERPT_CONTEXT_AFTER,
        max_chars=excerpt_max_chars,
    )
    service = _extract_service_name(raw_reason_line)
    config_path = _extract_config_path(service, subcode)
    return FailureDiagnostic(
        summary=summary,
        stage=stage,
        node=node,
        location=location,
        source_file=source,
        cause=cause,
        reason=reason_line,
        context=list(context),
        exit_code=exit_code,
        console_line=index + 1,
        console_byte_start=start,
        raw_log_excerpt=excerpt,
        recognized=True,
        code=err_code or CODE_JENKINS_BUILD_ERROR,
        subcode=subcode or "UNCLASSIFIED_CONSOLE_ERROR",
        remediation=remediation,
        service=service,
        config_path=config_path,
    )


def summarize_failure(
    console: str,
    *,
    start: int = 0,
    sensitive_values: Iterable[str] = (),
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
) -> str:
    """Backward-compatible one-line failure summary for alerts and build.error."""

    return diagnose_failure(
        console,
        start=start,
        sensitive_values=sensitive_values,
        summary_max_chars=summary_max_chars,
    ).summary




# Canonical failure_site values for BuildJob.failure_site / structured writers.
FAILURE_SITE_PRECHECK = "precheck"
FAILURE_SITE_VALIDATING = "validating"
FAILURE_SITE_TRIGGERING = "triggering"
FAILURE_SITE_JENKINS = "jenkins"
FAILURE_SITE_POLL_TIMEOUT = "poll_timeout"
FAILURE_SITE_DEPLOY = "deploy"
FAILURE_SITE_STATIC_CHECK = "static_check"
FAILURE_SITE_DYNAMIC_CHECK = "dynamic_check"
FAILURE_SITE_WORKER = "worker"


def structured_failure(
    *,
    failure_site: str,
    summary: str,
    build_id: str | None = None,
    stage: str | None = None,
    extra: Mapping[str, Any] | None = None,
    raw_log_excerpt: str | None = None,
    container_logs: Mapping[str, str] | None = None,
    log_source: str | None = None,
    recognized: bool = True,
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None, str]:
    """Build (error_detail, raw_log_excerpt, failure_logs, failure_site) for non-Jenkins fail paths."""

    detail: dict[str, Any] = {
        "failure_site": failure_site,
        "summary": summary,
        "recognized": recognized,
    }
    if build_id:
        detail["build_id"] = build_id
    if stage:
        detail["stage"] = stage
    if extra:
        for key, value in extra.items():
            if value is not None and key not in detail:
                detail[key] = value
    source = log_source or failure_site
    logs = None
    if raw_log_excerpt or container_logs:
        logs = failure_logs_payload(
            source=source,
            raw_log_excerpt=raw_log_excerpt,
            container_logs=container_logs,
        )
    elif failure_site:
        logs = {"source": source}
    return detail, raw_log_excerpt, logs, failure_site

def failure_logs_payload(
    *,
    source: str,
    raw_log_excerpt: str | None = None,
    container_logs: Mapping[str, str] | None = None,
    max_chars: int = DEFAULT_EXCERPT_MAX_CHARS,
) -> dict[str, Any]:
    """Shape optional log bags returned on build.failure_logs without breaking consumers."""

    payload: dict[str, Any] = {"source": source}
    if raw_log_excerpt:
        text = raw_log_excerpt if len(raw_log_excerpt) <= max_chars else raw_log_excerpt[: max_chars - 1] + "…"
        payload["raw_log_excerpt"] = text
    if container_logs:
        trimmed: dict[str, str] = {}
        remaining = max_chars
        for key, value in container_logs.items():
            if remaining <= 0:
                break
            chunk = value if len(value) <= remaining else value[: remaining - 1] + "…"
            trimmed[str(key)] = chunk
            remaining -= len(chunk)
        payload["container_logs"] = trimmed
    return payload
