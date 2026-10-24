from __future__ import annotations

from copy import deepcopy
import json
import os
import subprocess
import sys
import time
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from orchestrator.agent_executor import AgentExecutionError, CodexCliExecutor
from orchestrator.models import AgentRole, AgentRuntimeConfig


def role(**runtime: Any) -> AgentRole:
    return AgentRole(
        role_key="static_build_agent", prompt="检查目标路由并返回 JSON",
        output_schema={
            "type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"], "additionalProperties": False,
        },
        runtime_config=AgentRuntimeConfig(**runtime),
    )


class RecordingRunner:
    def __init__(self, output: str | bytes = '{"ok": true}', returncode: int = 0, *, raw_transport: bool = False):
        self.output = output
        self.returncode = returncode
        self.raw_transport = raw_transport
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.schema: Any = None

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        self.schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
        output_file = Path(command[command.index("--output-last-message") + 1])
        if isinstance(self.output, bytes):
            output_file.write_bytes(self.output)
        else:
            output = self.output if self.raw_transport else json.dumps({"response_json": self.output})
            output_file.write_text(output, encoding="utf-8")
        return subprocess.CompletedProcess(command, self.returncode, "sensitive stdout", "sensitive stderr")


def executor(runner: Any, **kwargs: Any) -> CodexCliExecutor:
    # The injected runner never executes Python as a CLI or contacts a model.
    return CodexCliExecutor(sys.executable, runner=runner, **kwargs)


def test_default_runtime_is_explicit_and_prompt_is_stdin_not_argv(tmp_path):
    runner = RecordingRunner()
    configured = role()
    context = {"require_route": "/检查", "secret": "private prompt context"}
    assert executor(runner).run_json(configured, context, cwd=tmp_path) == {"ok": True}
    command, kwargs = runner.calls[0]
    assert command[0] == str(Path(sys.executable).resolve())
    assert command[1] == "exec"
    assert "--ephemeral" in command
    assert command[command.index("-C") + 1] == str(tmp_path)
    assert 'approval_policy="never"' in command
    assert command[command.index("--sandbox") + 1] == "danger-full-access"
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "--skip-git-repo-check" in command
    assert "--model" not in command
    assert not any(item.startswith("model_reasoning_effort=") for item in command)
    assert command[-1] == "-"
    assert "private prompt context" not in repr(command)
    assert kwargs["input"].startswith(configured.prompt + "\n\nINPUT JSON:\n")
    context_text, response_contract = kwargs["input"].split("INPUT JSON:\n", 1)[1].split("\n\nRESPONSE VALUE JSON SCHEMA:\n", 1)
    assert json.loads(context_text) == context
    assert json.loads(response_contract.split("\n\nOUTPUT TRANSPORT CONTRACT:\n", 1)[0]) == configured.output_schema
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 900
    assert runner.schema == {
        "type": "object", "properties": {"response_json": {"type": "string"}},
        "required": ["response_json"], "additionalProperties": False,
    }
    schema_file = Path(command[command.index("--output-schema") + 1])
    output_file = Path(command[command.index("--output-last-message") + 1])
    assert schema_file.parent != tmp_path
    assert not schema_file.exists()
    assert not output_file.exists()


@pytest.mark.parametrize("authorization,sandbox,skip_git", list(product(
    ["never", "on-request", "untrusted"],
    ["danger-full-access", "workspace-write", "read-only"], [True, False],
)))
def test_all_authorization_sandbox_git_combinations_remain_independent(tmp_path, authorization, sandbox, skip_git):
    runner = RecordingRunner()
    executor(runner).run_json(role(
        authorization_mode=authorization, sandbox_mode=sandbox, skip_git_repo_check=skip_git,
    ), {}, cwd=tmp_path)
    command, _ = runner.calls[0]
    assert f"approval_policy={json.dumps(authorization)}" in command
    assert command[command.index("--sandbox") + 1] == sandbox
    assert ("--skip-git-repo-check" in command) is skip_git
    assert "--dangerously-bypass-approvals-and-sandbox" not in command


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_role_model_effort_and_timeout_are_forwarded_without_selecting_another_model(tmp_path, effort):
    runner = RecordingRunner()
    executor(runner).run_json(role(
        model="user-selected-model", reasoning_effort=effort, timeout_seconds=17,
    ), {}, cwd=tmp_path)
    command, kwargs = runner.calls[0]
    assert command[command.index("--model") + 1] == "user-selected-model"
    assert f"model_reasoning_effort={json.dumps(effort)}" in command
    assert kwargs["timeout"] == 17


def test_environment_inherits_runtime_auth_and_explicit_overrides_not_worker_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("CODEX_HOME", "/host/codex")
    monkeypatch.setenv("OPENAI_API_KEY", "host-model-auth")
    monkeypatch.setenv("HTTPS_PROXY", "http://host-proxy")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("JENKINS_PASSWORD", "worker-only-secret")
    monkeypatch.setenv("LD_PRELOAD", "untrusted-worker-preload")
    runner = RecordingRunner()
    executor(runner).run_json(role(), {}, cwd=tmp_path, env={
        "OPENAI_API_KEY": "explicit-model-auth", "APPLICATION_TOKEN": "explicit-app-secret",
    })
    actual = runner.calls[0][1]["env"]
    assert actual["HOME"] == "/host/home"
    assert actual["CODEX_HOME"] == "/host/codex"
    assert actual["OPENAI_API_KEY"] == "explicit-model-auth"
    assert actual["APPLICATION_TOKEN"] == "explicit-app-secret"
    assert actual["HTTPS_PROXY"] == "http://host-proxy"
    assert actual["LC_ALL"] == "C.UTF-8"
    assert "JENKINS_PASSWORD" not in actual
    assert "LD_PRELOAD" not in actual
    assert os.environ["OPENAI_API_KEY"] == "host-model-auth"
    assert "APPLICATION_TOKEN" not in os.environ


def test_codex_proxy_is_injected_only_into_the_codex_subprocess(tmp_path, monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    runner = RecordingRunner()
    executor(runner, https_proxy="http://codex-proxy.example:7890").run_json(role(), {}, cwd=tmp_path)
    child_environment = runner.calls[0][1]["env"]
    assert child_environment["HTTPS_PROXY"] == "http://codex-proxy.example:7890"
    assert "HTTPS_PROXY" not in os.environ


@pytest.mark.parametrize("credential", ["CODEX_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY"])
@pytest.mark.parametrize("value", ["", " \t\n"])
def test_blank_api_key_overrides_do_not_mask_persisted_cli_auth(tmp_path, monkeypatch, credential, value):
    monkeypatch.setenv("CODEX_HOME", "/worker/persisted-codex-login")
    monkeypatch.setenv(credential, value)
    runner = RecordingRunner()
    executor(runner).run_json(role(), {}, cwd=tmp_path)
    actual = runner.calls[0][1]["env"]
    assert actual["CODEX_HOME"] == "/worker/persisted-codex-login"
    assert credential not in actual
    assert os.environ[credential] == value


def test_explicit_blank_api_key_removes_an_inherited_key_only_for_the_child(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "host-model-auth")
    runner = RecordingRunner()
    executor(runner).run_json(role(), {}, cwd=tmp_path, env={"CODEX_API_KEY": ""})
    assert "CODEX_API_KEY" not in runner.calls[0][1]["env"]
    assert os.environ["CODEX_API_KEY"] == "host-model-auth"


@pytest.mark.parametrize("env", [
    {"": "secret"}, {"bad=name": "secret"}, {"bad\0name": "secret"},
    {"KEY": "secret\0value"}, {"KEY": 123}, {123: "secret"},
])
def test_invalid_environment_is_rejected_without_leaking_its_value(tmp_path, env):
    runner = RecordingRunner()
    with pytest.raises(AgentExecutionError, match="invalid variable") as error:
        executor(runner).run_json(role(), {}, cwd=tmp_path, env=env)
    assert "secret" not in str(error.value)
    assert runner.calls == []


def test_missing_cli_is_reported_without_attempting_a_run(tmp_path):
    runner = RecordingRunner()
    with pytest.raises(AgentExecutionError, match="not installed"):
        CodexCliExecutor("orchestrator-nonexistent-codex-cli", runner=runner).run_json(role(), {}, cwd=tmp_path)
    assert runner.calls == []


def test_nonexecutable_cli_is_rejected(tmp_path):
    path = tmp_path / "not-executable"
    path.write_text("not an executable")
    runner = RecordingRunner()
    with pytest.raises(AgentExecutionError, match="not installed"):
        CodexCliExecutor(str(path), runner=runner).run_json(role(), {}, cwd=tmp_path)
    assert runner.calls == []


@pytest.mark.parametrize("kind", ["missing", "file", "symlink-loop"])
def test_unavailable_working_directory_is_a_safe_error(tmp_path, kind):
    cwd = tmp_path / "unavailable"
    if kind == "file":
        cwd.write_text("not a directory")
    elif kind == "symlink-loop":
        cwd.symlink_to(cwd)
    runner = RecordingRunner()
    with pytest.raises(AgentExecutionError, match="working directory"):
        executor(runner).run_json(role(), {}, cwd=cwd)
    assert runner.calls == []


@pytest.mark.parametrize("failure", [
    OSError("sensitive process failure"), subprocess.TimeoutExpired("secret command", 1),
])
def test_start_and_timeout_failures_do_not_expose_prompt_or_underlying_details(tmp_path, failure):
    def fail(*args, **kwargs):
        raise failure

    with pytest.raises(AgentExecutionError) as error:
        executor(fail).run_json(role(), {"token": "sensitive prompt"}, cwd=tmp_path)
    assert "sensitive" not in str(error.value)
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__


def test_nonzero_exit_does_not_read_or_expose_agent_output(tmp_path):
    runner = RecordingRunner("sensitive malformed output", returncode=7)
    with pytest.raises(AgentExecutionError, match="did not complete") as error:
        executor(runner).run_json(role(), {}, cwd=tmp_path)
    assert "sensitive" not in str(error.value)
    assert not Path(runner.calls[0][0][runner.calls[0][0].index("--output-last-message") + 1]).exists()


@pytest.mark.parametrize("output", [
    "", "not json", '{"ok": true} trailing', '{"ok": true, "ok": false}',
    '{"ok": NaN}', '{"ok": Infinity}', b"\xff", "[" * 3000,
])
def test_malformed_nonstandard_or_deep_output_is_rejected(tmp_path, output):
    with pytest.raises(AgentExecutionError, match="invalid or oversized"):
        executor(RecordingRunner(output)).run_json(role(), {}, cwd=tmp_path)


@pytest.mark.parametrize("output", ["[]", "null", "1", '"private response"'])
def test_response_must_be_an_object_even_for_an_unconstrained_schema(tmp_path, output):
    configured = role().model_copy(update={"output_schema": {}})
    with pytest.raises(AgentExecutionError, match="JSON object"):
        executor(RecordingRunner(output)).run_json(configured, {}, cwd=tmp_path)


@pytest.mark.parametrize("output", [
    '{"ok": "private bad value"}', "{}", '{"ok": true, "unexpected": "private extra"}',
])
def test_output_is_validated_against_the_role_schema(tmp_path, output):
    with pytest.raises(AgentExecutionError, match="does not match") as error:
        executor(RecordingRunner(output)).run_json(role(), {}, cwd=tmp_path)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("role_key,value", [
    ("static_build_agent", {
        "route_found": True, "patch_available": False, "diff_list": [], "report_text": "",
        "probe_payload": {"protocol": "http", "path": "/ready", "method": "POST",
            "query_params": {"tag": ["one", "two"]}, "headers": {"X-Client": "自由字段"},
            "body": {"nested": {"list": [1, True, None, {"name": "例子"}]}},
            "assertions": {"expected_status_list": [200, 201]}},
    }),
    ("static_build_agent", {
        "route_found": False, "patch_available": True, "diff_list": ["diff --git a/a.py b/a.py\n+new route\n"],
        "report_text": "请先审核补丁", "probe_payload": None,
    }),
    ("dynamic_debug_agent", {
        "retry_payload": {"method": "POST", "body": {"arbitrary": [{"fields": "are retained"}]}},
        "report_text": "以正确 JSON 重试",
    }),
    ("dynamic_debug_agent", {"retry_payload": None, "report_text": "无法修复"}),
])
def test_default_role_free_form_objects_round_trip_through_a_strict_cli_schema(tmp_path, role_key, value):
    from orchestrator.route_checks import _default_role

    configured = _default_role(role_key)
    unchanged = deepcopy(configured.model_dump())
    runner = RecordingRunner(json.dumps(value, ensure_ascii=False))
    result = executor(runner).run_json(configured, {}, cwd=tmp_path)
    assert result == value
    assert configured.model_dump() == unchanged
    # The CLI/provider sees no open object or optional object properties,
    # while persistence still receives actual nested probe/retry dictionaries.
    assert runner.schema["additionalProperties"] is False
    assert runner.schema["required"] == list(runner.schema["properties"])
    assert runner.schema["properties"] == {"response_json": {"type": "string"}}
    payload_key = "probe_payload" if role_key == "static_build_agent" else "retry_payload"
    assert result[payload_key] is None or isinstance(result[payload_key], dict)


@pytest.mark.parametrize("schema,value", [
    ({"type": "object", "properties": {"required": {"type": "string"}, "optional": {"type": "integer"}},
      "required": ["required"], "additionalProperties": False}, {"required": "present"}),
    ({"type": "object", "additionalProperties": {"type": "string"}}, {"arbitrary-header": "value"}),
    ({"type": "object", "patternProperties": {"^key_": {"type": "integer", "minimum": 1}},
      "additionalProperties": False}, {"key_first": 1, "key_second": 2}),
    ({"anyOf": [
        {"type": "object", "properties": {"choice": {"const": 1}}, "required": ["choice"]},
        {"type": "object", "required": ["other"]},
    ]}, {"choice": 1}),
    ({"type": "object", "allOf": [{"required": ["name"]}, {"properties": {"name": {"type": "string"}}}]},
     {"name": "custom combination"}),
    ({"type": "object", "properties": {"name": {"type": "string"}, "details": {"type": ["null", "object"]}},
      "dependentRequired": {"details": ["name"]}}, {"name": "optional maps", "details": {"more": [True, None]}}),
    ({"type": "object", "if": {"required": ["primary"]}, "then": {"required": ["secondary"]}},
     {"primary": 1, "secondary": 2}),
])
def test_custom_general_json_schemas_are_not_restricted_to_the_provider_subset(tmp_path, schema, value):
    configured = role().model_copy(update={"output_schema": deepcopy(schema)})
    runner = RecordingRunner(json.dumps(value))
    assert executor(runner).run_json(configured, {}, cwd=tmp_path) == value
    assert configured.output_schema == schema
    prompt = runner.calls[0][1]["input"]
    embedded = prompt.split("RESPONSE VALUE JSON SCHEMA:\n", 1)[1].split("\n\nOUTPUT TRANSPORT CONTRACT:\n", 1)[0]
    assert json.loads(embedded) == schema
    assert runner.schema == {"type": "object", "properties": {"response_json": {"type": "string"}},
                             "required": ["response_json"], "additionalProperties": False}


@pytest.mark.parametrize("transport", [
    {}, {"ok": True}, {"response_json": None}, {"response_json": {}}, {"response_json": 1},
    {"response_json": "{\"ok\":true}", "extra": "private unexpected envelope value"},
])
def test_transport_shape_is_validated_without_falling_back_to_unwrapped_output(tmp_path, transport):
    runner = RecordingRunner(json.dumps(transport), raw_transport=True)
    with pytest.raises(AgentExecutionError, match="invalid structured output transport") as error:
        executor(runner).run_json(role(), {}, cwd=tmp_path)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("raw", [
    '{"response_json":"{}","response_json":"{\\\"ok\\\":true}"}',
    '{"response_json":NaN}', '{"response_json":"\\ud800"}',
])
def test_transport_duplicate_keys_non_json_constants_and_bad_unicode_are_rejected(tmp_path, raw):
    with pytest.raises(AgentExecutionError, match="invalid or oversized"):
        executor(RecordingRunner(raw, raw_transport=True)).run_json(role(), {}, cwd=tmp_path)


def test_business_result_can_itself_use_the_transport_property_name(tmp_path):
    schema = {"type": "object", "properties": {"response_json": {"type": "object"}}, "required": ["response_json"]}
    value = {"response_json": {"still": ["a normal", "business object"]}}
    configured = role().model_copy(update={"output_schema": schema})
    assert executor(RecordingRunner(json.dumps(value))).run_json(configured, {}, cwd=tmp_path) == value


def test_inner_result_is_decoded_once_and_must_be_a_json_object(tmp_path):
    twice_encoded = json.dumps('{"ok":true}')
    with pytest.raises(AgentExecutionError, match="JSON object"):
        executor(RecordingRunner(twice_encoded)).run_json(role(), {}, cwd=tmp_path)


def test_surrogate_in_an_inner_business_string_is_rejected_before_persistence(tmp_path):
    configured = role().model_copy(update={"output_schema": {"type": "object"}})
    with pytest.raises(AgentExecutionError, match="invalid or oversized"):
        executor(RecordingRunner('{"text":"\\ud800"}')).run_json(configured, {}, cwd=tmp_path)


@pytest.mark.parametrize("schema", [
    {"type": "nonexistent-private-type"}, {"required": "private-string"},
    {"$schema": "https://invalid.example/private-meta-schema"}, {"const": float("nan")},
])
def test_invalid_or_unknown_schema_is_rejected_before_cli_runs(tmp_path, schema):
    configured = role().model_copy(update={"output_schema": schema})
    runner = RecordingRunner()
    with pytest.raises(AgentExecutionError, match="schema is invalid") as error:
        executor(runner).run_json(configured, {}, cwd=tmp_path)
    assert "private" not in str(error.value)
    assert runner.calls == []


@pytest.mark.parametrize("schema", [
    {"$defs": {"result": {"type": "object"}}, "$ref": "#/$defs/result"},
    {"$schema": "http://json-schema.org/draft-07/schema#", "definitions": {"result": {"type": "object"}}, "$ref": "#/definitions/result"},
])
def test_local_schema_references_and_known_dialects_are_supported(tmp_path, schema):
    configured = role().model_copy(update={"output_schema": schema})
    assert executor(RecordingRunner()).run_json(configured, {}, cwd=tmp_path) == {"ok": True}


def test_external_schema_references_never_fetch_network_or_local_files(tmp_path, monkeypatch):
    import urllib.request

    fetched = []

    def refuse(*args, **kwargs):
        fetched.append(args)
        raise AssertionError("schema validation must not fetch resources")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    for ref in ("https://schema.invalid/private.json", (tmp_path / "private.json").as_uri()):
        configured = role().model_copy(update={"output_schema": {"$ref": ref}})
        with pytest.raises(AgentExecutionError, match="does not match"):
            executor(RecordingRunner()).run_json(configured, {}, cwd=tmp_path)
    assert fetched == []


@pytest.mark.parametrize("context", [{"not-json": object()}, {"number": float("nan")}, {"value": "\ud800"}])
def test_non_json_context_fails_safely_before_execution(tmp_path, context):
    runner = RecordingRunner()
    with pytest.raises(AgentExecutionError, match="context is invalid"):
        executor(runner).run_json(role(), context, cwd=tmp_path)
    assert runner.calls == []


def test_prompt_and_schema_sizes_are_bounded(tmp_path):
    runner = RecordingRunner()
    with pytest.raises(AgentExecutionError, match="context is invalid"):
        executor(runner).run_json(role(), {"large": "x" * (2 * 1024 * 1024)}, cwd=tmp_path)
    large_schema = role().model_copy(update={"output_schema": {"description": "x" * (1024 * 1024)}})
    with pytest.raises(AgentExecutionError, match="schema is invalid"):
        executor(runner).run_json(large_schema, {}, cwd=tmp_path)
    assert runner.calls == []


def test_structured_output_has_a_byte_limit(tmp_path):
    runner = RecordingRunner('{"ok": true}' + " " * 100)
    with pytest.raises(AgentExecutionError, match="oversized"):
        executor(runner, max_output_bytes=32).run_json(role(), {}, cwd=tmp_path)
    valid = '{"ok": true}'
    wire_size = len(json.dumps({"response_json": valid}).encode("utf-8"))
    assert executor(RecordingRunner(valid), max_output_bytes=wire_size).run_json(role(), {}, cwd=tmp_path) == {"ok": True}


@pytest.mark.parametrize("limit", [0, -1, True, 2.5, "100"])
def test_output_limit_requires_a_positive_integer(limit):
    with pytest.raises(ValueError, match="positive integer"):
        CodexCliExecutor(max_output_bytes=limit)


def test_control_files_never_overwrite_workspace_symlinks(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    for name in ("schema.json", "response.json"):
        (cwd / name).symlink_to(outside)
    runner = RecordingRunner()
    assert executor(runner).run_json(role(), {}, cwd=cwd) == {"ok": True}
    assert outside.read_text() == "untouched"
    assert (cwd / "schema.json").is_symlink()
    assert (cwd / "response.json").is_symlink()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "missing"])
def test_response_cannot_redirect_reads_to_a_link_or_special_file(tmp_path, kind):
    protected = tmp_path / "protected.json"
    protected.write_text('{"ok": true}')

    def unsafe(command, **kwargs):
        output = Path(command[command.index("--output-last-message") + 1])
        if kind == "symlink":
            output.symlink_to(protected)
        elif kind == "hardlink":
            output.hardlink_to(protected)
        elif kind == "fifo":
            os.mkfifo(output)
        elif kind == "directory":
            output.mkdir()
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(AgentExecutionError, match="invalid or oversized"):
        executor(unsafe).run_json(role(), {}, cwd=tmp_path)
    assert protected.read_text() == '{"ok": true}'


def stub_cli(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "codex-stub"
    path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
    path.chmod(0o700)
    return path


def test_local_process_integration_passes_prompt_cwd_env_without_shell_or_output_capture(tmp_path):
    cli = stub_cli(tmp_path, """
import json, os, sys
from pathlib import Path
prompt = sys.stdin.read()
assert sys.argv[1] == 'exec' and sys.argv[-1] == '-'
schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
assert schema['type'] == 'object'
result = {'cwd': os.getcwd(), 'token': os.environ['APP_TOKEN'], 'prompt': prompt}
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps({'response_json': json.dumps(result)}))
sys.stdout.write('private stdout' * 100000)
sys.stderr.write('private stderr' * 100000)
""")
    cwd = tmp_path / "repo with spaces"
    cwd.mkdir()
    configured = role().model_copy(update={"output_schema": {"type": "object"}})
    result = CodexCliExecutor(str(cli)).run_json(
        configured, {"route": "/ready", "literal": "$(touch should-not-exist)"},
        cwd=cwd, env={"APP_TOKEN": "explicit integration token"},
    )
    assert result["cwd"] == str(cwd)
    assert result["token"] == "explicit integration token"
    assert "$(touch should-not-exist)" in result["prompt"]
    assert not (cwd / "should-not-exist").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are used in deployment")
def test_local_timeout_kills_cli_and_descendants_without_leaking_details(tmp_path):
    pid_file = tmp_path / "pids"
    cli = stub_cli(tmp_path, """
import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(os.environ['PID_FILE']).write_text(str(os.getpid()) + ' ' + str(child.pid))
sys.stdout.write('private CLI token')
sys.stdout.flush()
time.sleep(60)
""")
    started = time.monotonic()
    with pytest.raises(AgentExecutionError, match="exceeded its configured timeout") as error:
        CodexCliExecutor(str(cli)).run_json(
            role(timeout_seconds=1), {"private": "context secret"},
            cwd=tmp_path, env={"PID_FILE": str(pid_file)},
        )
    assert time.monotonic() - started < 10
    assert "private" not in str(error.value)
    cli_pid, child_pid = map(int, pid_file.read_text().split())
    assert not Path(f"/proc/{cli_pid}").exists()  # The direct child is reaped.
    # A container's PID 1 may not immediately reap orphan zombies.  A zombie
    # has exited and cannot keep working or holding deployment resources.
    for _ in range(100):
        state_file = Path(f"/proc/{child_pid}/stat")
        if not state_file.exists() or state_file.read_text().split()[2] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("Codex descendant survived the configured timeout")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are used in deployment")
def test_helpers_cannot_continue_running_after_a_successful_cli_exit(tmp_path):
    pid_file = tmp_path / "helper-pid"
    cli = stub_cli(tmp_path, """
import json, os, subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(os.environ['PID_FILE']).write_text(str(child.pid))
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps({'response_json': '{"ok": true}'}))
""")
    assert CodexCliExecutor(str(cli)).run_json(role(), {}, cwd=tmp_path, env={"PID_FILE": str(pid_file)}) == {"ok": True}
    child_pid = int(pid_file.read_text())
    for _ in range(100):
        state_file = Path(f"/proc/{child_pid}/stat")
        if not state_file.exists() or state_file.read_text().split()[2] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("Codex helper survived a completed executor run")
