from base64 import b64encode
import json
from urllib.parse import quote

import pytest

from orchestrator.jenkins_diagnostics import redact_console, summarize_failure


def test_checkout_failure_keeps_original_stage_and_evidence_not_skipped_stages():
    console = "\n".join([
        "Running on builder in /opt/jenkins-agent/workspace/app",
        "[Pipeline] stage",
        "[Pipeline] { (Checkout)",
        "[2026-10-16T03:50:42.971Z] fatal: unable to access 'https://gitlab.example/app.git/': Could not resolve host: gitlab.example",
        "[Pipeline] }",
        "[Pipeline] // stage",
        "[Pipeline] { (Build And Push)",
        'Stage "Build And Push" skipped due to earlier failure(s)',
        "[Pipeline] // stage",
        "[Pipeline] { (Declarative: Post Actions)",
        "Archiving artifacts",
        "[Pipeline] // stage",
        "ERROR: script returned exit code 128",
        "Finished: FAILURE",
    ])
    summary = summarize_failure(console)
    assert "阶段=Checkout" in summary
    assert "节点=builder" in summary
    assert "位置=console L4" in summary
    assert "原因=DNS 解析失败：fatal: unable to access" in summary
    assert "退出码=128" in summary
    assert "Build And Push" not in summary
    assert "Post Actions" not in summary


def test_progressive_text_console_notes_and_timeout_setup_are_not_failures():
    note = "\x1b[8mha:////signed-console-annotation\x1b[0m"
    console = "\n".join([
        f"{note}Running on {note}builder in /opt/jenkins-agent/workspace/app",
        f"{note}[Pipeline] timeout",
        "[2026-10-16T03:50:41.106Z] Timeout set to expire in 30 min",
        f"{note}[Pipeline] {{ (Checkout)",
        "fatal: Could not resolve host: gitlab.example",
        f"{note}[Pipeline] // stage",
        "ERROR: script returned exit code 128",
    ])
    summary = summarize_failure(console)
    assert "阶段=Checkout" in summary
    assert "节点=builder" in summary
    assert "位置=console L5" in summary
    assert "DNS 解析失败" in summary
    assert "signed-console-annotation" not in summary
    assert "Timeout set to expire" not in summary


@pytest.mark.parametrize(("error", "cause"), [
    ("ERROR: failed to solve: x509: certificate signed by unknown authority", "TLS 证书校验失败"),
    ("fatal: Authentication failed for 'https://gitlab.example/team/app.git'", "认证或权限失败"),
    ("denied: requested access to the resource is denied", "认证或权限失败"),
    ("dial tcp 10.0.0.5:443: connect: connection refused", "连接失败"),
    ("ERROR: failed to solve: failed to read dockerfile: open Dockerfile: no such file or directory", "文件或路径缺失"),
    ("npm error ENOENT Could not read package.json", "文件或路径缺失"),
    ("npm error code EBADENGINE", "依赖运行时版本不兼容"),
    ("Error response from daemon: no space left on device", "存储空间不足"),
    ("Error response from daemon: port is already allocated", "端口冲突"),
])
def test_failure_cause_classifies_observed_errors_without_inventing_a_reason(error, cause):
    summary = summarize_failure("[Pipeline] { (Build And Push)\n" + error + "\nERROR: script returned exit code 1")
    assert "阶段=Build And Push" in summary
    assert cause in summary
    assert error in summary


def test_node_engine_failure_includes_required_and_actual_versions():
    console = "\n".join([
        "[Pipeline] { (Build And Push)",
        "#9 1.2 npm error code EBADENGINE",
        "#9 1.2 npm error engine Unsupported engine",
        "#9 1.2 npm error engine Not compatible with your version of node/npm: vite@7.1.0",
        '#9 1.2 npm error notsup Required: {"node":"^20.19.0 || >=22.12.0"}',
        '#9 1.2 npm error notsup Actual: {"npm":"10.9.0","node":"v22.11.0"}',
        "Dockerfile:5",
        "ERROR: failed to solve: process did not complete successfully: exit code: 1",
    ])
    summary = summarize_failure(console)
    assert "依赖运行时版本不兼容" in summary
    assert "Dockerfile:5" in summary
    assert ">=22.12.0" in summary
    assert "v22.11.0" in summary


def test_compile_and_python_errors_preserve_file_and_line_information():
    compile_error = "src/main.tsx(17,5): error TS2322: Type 'string' is not assignable to type 'number'."
    assert compile_error in summarize_failure("[Pipeline] { (Build And Push)\n" + compile_error)
    traceback = '\n'.join([
        "[Pipeline] { (Validate Compose)",
        "Traceback (most recent call last):",
        '  File "validate-compose.py", line 42, in validate',
        "ValueError: service web uses env_file; resolve it into environment before delivery",
    ])
    summary = summarize_failure(traceback)
    assert "Validate Compose" in summary
    assert "uses env_file" in summary
    assert "validate-compose.py:42" in summary
    assert "原因=ValueError:" in summary


def test_cleanup_error_does_not_replace_original_business_failure():
    console = "\n".join([
        "[Pipeline] { (Build And Push)",
        "src/main.tsx(17,5): error TS2322: incompatible type",
        "[Pipeline] // stage",
        "[Pipeline] { (Declarative: Post Actions)",
        "ERROR: deleteDir failed: permission denied",
        "[Pipeline] // stage",
        "ERROR: script returned exit code 2",
    ])
    summary = summarize_failure(console)
    assert "阶段=Build And Push" in summary
    assert "TS2322" in summary
    assert "Post Actions" not in summary
    assert "deleteDir" not in summary


def test_apport_exception_hook_does_not_replace_primary_buildkit_failure():
    console = "\n".join([
        "[Pipeline] { (Build And Push)",
        "#14 0.269 Internal Error: Error when performing the request to https://registry.npmjs.org/pnpm",
        "#14 ERROR: process did not complete successfully: exit code: 1",
        "ERROR: failed to build: failed to solve: process exited with code 1",
        "Traceback (most recent call last):",
        "subprocess.CalledProcessError: docker build returned non-zero exit status 1",
        "Error in sys.excepthook:",
        "Traceback (most recent call last):",
        '  File "/usr/lib/python3/dist-packages/apport_python_hook.py", line 114, in apport_excepthook',
        "FileNotFoundError: temporary build path no longer exists",
        "Original exception was:",
        "Traceback (most recent call last):",
        "subprocess.CalledProcessError: docker build returned non-zero exit status 1",
        "[Pipeline] // stage",
        "ERROR: script returned exit code 1",
    ])

    summary = summarize_failure(console)

    assert "阶段=Build And Push" in summary
    assert "包管理器注册表请求失败" in summary
    assert "registry.npmjs.org/pnpm" in summary
    assert "apport_python_hook.py" not in summary
    assert "FileNotFoundError" not in summary


def test_warnings_and_zero_failure_counts_do_not_win_over_the_error():
    console = "\n".join([
        "[Pipeline] { (Build And Push)",
        "npm warn deprecated pkg: previous release failed to resolve a dependency",
        "Tests: 0 failures",
        "src/main.tsx(17,5): error TS2322: incompatible type",
    ])
    summary = summarize_failure(console)
    assert "原因=src/main.tsx(17,5): error TS2322" in summary
    assert "deprecated" not in summary


def test_nested_stages_keep_failed_service_context():
    console = "[Pipeline] { (Build And Push)\n[Pipeline] { (api)\nERROR: missing application input\n[Pipeline] // stage\n[Pipeline] // stage"
    assert "阶段=Build And Push / api" in summarize_failure(console)


@pytest.mark.parametrize("error", [
    "COMPOSE_JSON must contain a non-empty services mapping",
    "APPID contains unsupported characters",
    "invalid service name: 'api_invalid'",
    "service api has unsafe build context",
    "service api workspace build context or Dockerfile does not exist",
    "Reserved build workspace path already exists in repository",
    "Pushed build image digest could not be verified",
])
def test_pipeline_validation_errors_without_error_prefix_preserve_the_real_reason(error):
    summary = summarize_failure("[Pipeline] { (Validate Compose)\n" + error + "\n[Pipeline] // stage\nERROR: script returned exit code 1")
    assert "阶段=Validate Compose" in summary
    assert error in summary


def test_very_long_lines_and_json_escaped_inputs_are_safely_bounded():
    secret = 'multiline\\quoted"input'
    console = "ERROR: " + "x" * (256 * 1024) + " " + json.dumps(secret)
    summary = summarize_failure(console, sensitive_values=[secret])
    assert len(summary) <= 1200
    assert json.dumps(secret)[1:-1] not in summary


def test_empty_or_uninformative_logs_are_honest_and_tail_line_numbers_are_not_absolute():
    assert "未识别出具体错误" in summarize_failure("")
    assert "未识别出具体错误" in summarize_failure('Stage "Build" skipped due to earlier failure(s)\nFinished: FAILURE')
    summary = summarize_failure("fatal: unknown application error", start=100000)
    assert "尾部片段 L1" in summary
    assert "字节起点 100000" in summary
    assert "阶段=" not in summary


def test_redaction_removes_credentials_without_changing_console_line_numbers():
    value = "app/private+credential"
    console = "\n".join([
        "-----BEGIN PRIVATE KEY-----",
        "private-key-data",
        "-----END PRIVATE KEY-----",
        "[Pipeline] { (Checkout)",
        "\x1b[31mfatal: Could not resolve host: gitlab.example\x1b[0m",
        "https://oauth2:url-secret@gitlab.example/app.git?access_token=query-secret",
        "Authorization: Bearer bearer-secret TOKEN=inline-secret PASSWORD='with spaces'",
        f"environment value: {value} {quote(value, safe='')} {b64encode(value.encode()).decode()}",
    ])
    safe = redact_console(console, [value])
    for secret in (value, quote(value, safe=""), b64encode(value.encode()).decode(), "private-key-data", "url-secret", "query-secret", "bearer-secret", "inline-secret", "with spaces"):
        assert secret not in safe
    assert safe.count("\n") == console.count("\n")
    summary = summarize_failure(console, sensitive_values=[value])
    assert "console L5" in summary
    assert "\x1b" not in summary


def test_summary_is_bounded_and_does_not_truncate_secrets_before_redaction():
    secret = "credential-that-must-not-survive"
    summary = summarize_failure("ERROR: " + "x" * 400 + " password=" + secret + " " + "y" * 2000)
    assert len(summary) <= 1200
    assert secret not in summary
