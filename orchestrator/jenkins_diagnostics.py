"""Bounded, redacted diagnostics derived from Jenkins console evidence."""

from __future__ import annotations

import json
import re
from base64 import b64encode
from collections.abc import Iterable
from urllib.parse import quote


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONSOLE_NOTE = re.compile(r"\x1b\[8mha:.*?\x1b\[0m", re.DOTALL)
_TIMESTAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2}T[^\]]+\]\s*")
_STAGE = re.compile(r"\[Pipeline\]\s*\{\s*\((.+)\)\s*$")
_EXCEPTION_HOOK_START = re.compile(r"^Error in sys\.excepthook:")
_EXCEPTION_HOOK_END = re.compile(r"^Original exception was:")
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
_CAUSES = (
    ("包管理器注册表请求失败", r"error when performing the request to https?://registry\."),
    ("DNS 解析失败", r"could not resolve host|couldn't resolve host|name or service not known|temporary failure in name resolution|\bENOTFOUND\b|\bEAI_AGAIN\b"),
    ("TLS 证书校验失败", r"\bx509:|SSL certificate problem|certificate verify failed|certificate signed by unknown authority|unable to (?:get local issuer|verify the first) certificate"),
    ("依赖运行时版本不兼容", r"\bEBADENGINE\b|unsupported engine|not compatible with your version of node"),
    ("连接失败", r"connection refused|connection timed out|connect ETIMEDOUT|network is unreachable|no route to host"),
    ("认证或权限失败", r"authentication failed|unauthorized|access denied|permission denied|requested access.+denied|denied:|status(?: code)?[: =]+(?:401|403)\b"),
    ("文件或路径缺失", r"no such file|\bENOENT\b|cannot find module|(?:dockerfile|package\.json).+(?:not found|does not exist)"),
    ("存储空间不足", r"no space left on device|\bENOSPC\b"),
    ("端口冲突", r"address already in use|port is already allocated"),
)


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


def summarize_failure(console: str, *, start: int = 0, sensitive_values: Iterable[str] = ()) -> str:
    lines = [_TIMESTAMP.sub("", line).strip() for line in redact_console(console, sensitive_values).splitlines()]
    exception_hook_lines = set()
    in_exception_hook = False
    for index, line in enumerate(lines):
        if _EXCEPTION_HOOK_START.search(line):
            in_exception_hook = True
        if in_exception_hook and not _EXCEPTION_HOOK_END.search(line):
            exception_hook_lines.add(index)
        elif in_exception_hook:
            in_exception_hook = False
    stages = []
    node = ""
    candidates = []
    exit_code = ""
    for index, line in enumerate(lines):
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
        code = re.search(r"(?:script returned exit code|exit code[: ]+)\s*(\d+)", line, re.IGNORECASE)
        if code and not exit_code:
            exit_code = code.group(1)
        if not line or line.startswith(("[Pipeline]", "+ ", "Finished:", "Stage \"")):
            continue
        if line.startswith("Timeout set to expire in "):
            continue
        if re.search(r"\bnpm warn\b|^warning:|\b0 (?:errors|failures|failed)\b", line, re.IGNORECASE):
            continue
        cause = next((label for label, pattern in _CAUSES if re.search(pattern, line, re.IGNORECASE)), "")
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
        candidates.append((priority, index, " / ".join(stages), node, cause))
    if not candidates:
        return "日志中未识别出具体错误；请查看该 Jenkins 构建的控制台"
    primary = [candidate for candidate in candidates if "Declarative: Post Actions" not in candidate[2]]
    if primary:
        failed_stage = primary[0][2]
        candidates = [candidate for candidate in primary if candidate[2] == failed_stage]
    _, index, stage, node, cause = max(candidates, key=lambda candidate: candidate[0])
    parts = []
    if stage:
        parts.append("阶段=" + _shorten(stage, 120))
    if node:
        parts.append("节点=" + _shorten(node, 60))
    location = f"console L{index + 1}"
    if start:
        location = f"console 尾部片段 L{index + 1}（片段内行号，字节起点 {start}）"
    nearby = lines[max(0, index - 8) : index + 15]
    source = ""
    for line in nearby:
        match = re.search(r"(?<![\w./-])[\w./-]*Dockerfile[\w.-]*:\d+", line)
        if match:
            source = match.group(0)
            break
    if not source:
        for line in reversed(lines[max(0, index - 8) : index + 1]):
            match = re.search(r'File "([^\"]+)", line (\d+)', line)
            if match:
                source = f"{match.group(1)}:{match.group(2)}"
                break
    parts.append("位置=" + (_shorten(source, 120) + "，" if source else "") + location)
    reason = _shorten(lines[index], 480)
    parts.append("原因=" + (cause + "：" if cause else "") + reason)
    context = []
    for line in lines[index + 1 : index + 9]:
        if not line or line.startswith("[Pipeline]") or line.startswith("Stage ") or line.startswith("Finished:"):
            break
        if line == lines[index] or _WRAPPER.search(line):
            continue
        context.append(_shorten(line, 160))
    requirements = [line for line in context if re.search(r"\b(?:required|actual):", line, re.IGNORECASE)]
    context = (requirements or context)[:2]
    if context:
        parts.append("上下文=" + " / ".join(context))
    if exit_code:
        parts.append("退出码=" + exit_code)
    return _shorten("；".join(parts), 1200)
