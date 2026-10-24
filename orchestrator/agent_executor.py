"""Small, injectable adapter for non-interactive local Codex CLI runs.

The CLI runs on the host, not in an intermediate shell.  Role configuration is
passed explicitly so an approval setting never silently changes the sandbox
setting.  Output and errors from the CLI are deliberately not included in
exceptions: they can contain prompts, credentials, or application logs.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import NoSuchResource

from orchestrator.models import AgentRole
from orchestrator.codex_config import CodexProviderConfig, codex_cli_overrides


class AgentExecutionError(RuntimeError):
    """A safe, user-facing Codex CLI execution failure."""


# A worker normally also holds database, registry, and Jenkins credentials.
# Inherit the host settings needed by the CLI/authentication, not all worker
# secrets.  Callers can explicitly supply additional application variables.
_INHERITED_ENVIRONMENT = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ",
    "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "PATHEXT", "APPDATA",
    "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
    "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "SSH_AUTH_SOCK",
})
_INHERITED_PREFIXES = ("LC_", "CODEX_", "OPENAI_", "AZURE_OPENAI_")
_AUTHENTICATION_ENVIRONMENT = frozenset({
    "CODEX_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY",
})
_MAX_SCHEMA_BYTES = 1024 * 1024
_MAX_PROMPT_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024 * 1024

# Structured Outputs accepts only a strict JSON Schema subset (not arbitrary
# dictionaries, optional properties, or every JSON Schema combination). Roles
# intentionally use general schemas, notably free-form probe bodies/headers.
# Keep that business contract unchanged and validate it locally; only this
# fixed, strict transport schema is sent to the CLI/provider.
_TRANSPORT_SCHEMA = {
    "type": "object",
    "properties": {"response_json": {"type": "string"}},
    "required": ["response_json"],
    "additionalProperties": False,
}
_TRANSPORT_SCHEMA_TEXT = json.dumps(_TRANSPORT_SCHEMA)
_TRANSPORT_VALIDATOR = Draft202012Validator(_TRANSPORT_SCHEMA)


def _strict_json(text: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> Any:
        raise ValueError("non-JSON numeric constant")

    value = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    # Escaped lone surrogates are accepted by Python's parser but cannot be
    # persisted or served as UTF-8 JSON. Reject them at the execution boundary.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return value


def _deny_external_schema(uri: str) -> Any:
    # Validation must not fetch schemas from an agent-controlled URL or read
    # local files.  Local $defs/$ref and embedded resource IDs still work.
    raise NoSuchResource(ref=uri)


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    """Stop descendants too, including ones left behind by an exited CLI."""

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:  # pragma: no cover - Linux is the deployment target
            process.kill()
    except ProcessLookupError:
        pass


def _run_process_group(
    command: Sequence[str], *, input: str, timeout: int, check: bool = False, **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """A subprocess.run-like runner that reaps/kills the whole local session.

    Discarded stdout/stderr avoid unbounded memory or log-file growth.  The
    only returned data is read separately from the bounded response file.
    """

    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, start_new_session=os.name == "posix",
        shell=False, close_fds=True, **kwargs,
    )
    try:
        process.communicate(input=input, timeout=timeout)
    finally:
        # Also clean up helpers a CLI may leave behind on normal/nonzero exit.
        # The process owns a new POSIX session, so this cannot kill the worker.
        _kill_process_group(process)
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        process.wait()
    completed = subprocess.CompletedProcess(command, process.returncode)
    if check and completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, command)
    return completed


class CodexCliExecutor:
    def __init__(
        self,
        executable: str = "codex",
        *,
        runner: Any | None = None,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        provider_config: Any | None = None,
        require_provider: bool = False,
        https_proxy: str | None = None,
    ) -> None:
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes < 1:
            raise ValueError("max_output_bytes must be a positive integer")
        self.executable = executable
        self.runner = runner if runner is not None else _run_process_group
        self.max_output_bytes = max_output_bytes
        self.provider_config = provider_config
        self.require_provider = require_provider
        self.https_proxy = https_proxy

    @staticmethod
    def _environment(env: Mapping[str, str] | None) -> dict[str, str]:
        process_env = {
            key: value for key, value in os.environ.items()
            if key in _INHERITED_ENVIRONMENT or key.startswith(_INHERITED_PREFIXES)
        }
        process_env.setdefault("PATH", os.defpath)
        if env is not None:
            for key, value in env.items():
                if (
                    not isinstance(key, str) or not key or "=" in key or "\0" in key
                    or not isinstance(value, str) or "\0" in value
                ):
                    raise AgentExecutionError("Agent environment contains an invalid variable")
                process_env[key] = value
        # Compose supplies an empty API key when operators use a persisted
        # CLI login instead. An absent override leaves that login available;
        # an explicitly present but empty credential is not useful to a CLI.
        for key in _AUTHENTICATION_ENVIRONMENT:
            if key in process_env and not process_env[key].strip():
                process_env.pop(key)
        return process_env

    def _executable(self, process_env: Mapping[str, str]) -> str:
        try:
            located = shutil.which(self.executable, path=process_env.get("PATH"))
            if located:
                return str(Path(located).resolve(strict=True))
        except (OSError, ValueError, RuntimeError, TypeError):
            pass
        raise AgentExecutionError("Codex CLI is not installed or not available on PATH")

    @staticmethod
    def _schema(role: AgentRole) -> tuple[str, Any]:
        try:
            schema_text = json.dumps(role.output_schema, ensure_ascii=False, allow_nan=False)
            if len(schema_text.encode("utf-8")) > _MAX_SCHEMA_BYTES:
                raise ValueError("schema size limit")
            validator_type = validator_for(role.output_schema, default=None)
            if validator_type is None:
                if "$schema" in role.output_schema:
                    raise ValueError("unsupported schema dialect")
                validator_type = Draft202012Validator
            validator_type.check_schema(role.output_schema)
            validator = validator_type(
                role.output_schema, registry=Registry(retrieve=_deny_external_schema),
            )
        except Exception:
            raise AgentExecutionError("Agent output schema is invalid or unsupported") from None
        return schema_text, validator

    def _read_response(self, output_file: Path) -> dict[str, Any]:
        try:
            # The control files live in a new private directory outside the
            # workspace.  Do not follow an output symlink or block on a FIFO
            # even if the CLI/repository replaced the response path.
            flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(output_file, flags)
            with os.fdopen(descriptor, "rb") as response_file:
                metadata = os.fstat(response_file.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError("unsafe response file")
                if metadata.st_size > self.max_output_bytes:
                    raise ValueError("response size limit")
                raw = response_file.read(self.max_output_bytes + 1)
            if len(raw) > self.max_output_bytes:
                raise ValueError("response size limit")

            response = _strict_json(raw.decode("utf-8"))
        except (OSError, ValueError, RecursionError, UnicodeError):
            raise AgentExecutionError("Codex CLI returned invalid or oversized structured output") from None
        if not isinstance(response, dict):
            raise AgentExecutionError("Codex CLI response must be a JSON object")
        return response

    @staticmethod
    def _decode_response(transport: dict[str, Any]) -> dict[str, Any]:
        try:
            _TRANSPORT_VALIDATOR.validate(transport)
        except Exception:
            raise AgentExecutionError("Codex CLI returned an invalid structured output transport") from None
        try:
            response = _strict_json(transport["response_json"])
        except (ValueError, RecursionError, UnicodeError):
            raise AgentExecutionError("Codex CLI returned invalid or oversized structured output") from None
        if not isinstance(response, dict):
            raise AgentExecutionError("Codex CLI response must be a JSON object")
        return response

    def run_json(
        self,
        role: AgentRole,
        context: Mapping[str, Any],
        *,
        cwd: str | Path,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        schema_text, validator = self._schema(role)
        process_env = self._environment(env)
        if self.https_proxy:
            process_env["HTTPS_PROXY"] = self.https_proxy
        provider = None
        if self.provider_config is not None:
            if hasattr(self.provider_config, "resolve"):
                provider, _ = self.provider_config.resolve(required=self.require_provider)
            elif callable(self.provider_config):
                provider = self.provider_config()
            if provider is None and self.require_provider:
                raise AgentExecutionError("Codex provider configuration is unavailable")
            if provider is not None:
                if not isinstance(provider, CodexProviderConfig):
                    raise AgentExecutionError("Codex provider configuration is invalid")
                process_env["CODEX_API_KEY"] = provider.api_key.get_secret_value()
        executable = self._executable(process_env)
        try:
            workdir = Path(cwd).resolve(strict=True)
            if not workdir.is_dir():
                raise ValueError("not a directory")
        except (OSError, ValueError, RuntimeError, TypeError):
            raise AgentExecutionError("Agent working directory does not exist or is unavailable") from None
        try:
            prompt = role.prompt + "\n\nINPUT JSON:\n" + json.dumps(
                dict(context), ensure_ascii=False, sort_keys=True, allow_nan=False,
            )
            prompt += (
                "\n\nRESPONSE VALUE JSON SCHEMA:\n" + schema_text
                + "\n\nOUTPUT TRANSPORT CONTRACT:\n"
                "Produce the business result as a JSON object satisfying RESPONSE VALUE JSON SCHEMA above. "
                "For the CLI final response, return exactly one object with the required string property "
                "response_json. Encode the entire business result with JSON.stringify (or json.dumps) "
                "into that string, preserving nested objects, arrays, nulls and scalar types. "
                "Do not add Markdown fences, commentary, or extra envelope properties. "
                'Example transport for a business result {"ok":true}: '
                + json.dumps({"response_json": '{"ok":true}'})
            )
            if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
                raise ValueError("prompt size limit")
        except (TypeError, ValueError, RecursionError, UnicodeError):
            raise AgentExecutionError("Agent context is invalid or exceeds its size limit") from None

        try:
            with tempfile.TemporaryDirectory(prefix="orchestrator-codex-") as temp_dir:
                schema_file = Path(temp_dir) / "schema.json"
                output_file = Path(temp_dir) / "response.json"
                # Exclusive creation avoids accidentally truncating another
                # file even if a control path is unexpectedly replaced.
                with schema_file.open("x", encoding="utf-8") as file:
                    file.write(_TRANSPORT_SCHEMA_TEXT)
                command = [
                    executable, "exec", "-C", str(workdir),
                    "--ephemeral",
                    "--output-schema", str(schema_file),
                    "--output-last-message", str(output_file),
                ]
                config = role.runtime_config
                command.extend([
                    "-c", f"approval_policy={json.dumps(config.authorization_mode)}",
                    "--sandbox", config.sandbox_mode,
                ])
                if config.skip_git_repo_check:
                    command.append("--skip-git-repo-check")
                if provider is not None:
                    command.extend(codex_cli_overrides(provider))
                model = config.model or (provider.default_model if provider is not None else None)
                reasoning_effort = config.reasoning_effort or (provider.default_effort if provider is not None else None)
                if model:
                    command.extend(["--model", model])
                if reasoning_effort:
                    command.extend(["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"])
                # Never expose application logs, environment secrets or the
                # prompt itself in argv/process listings.
                command.append("-")
                try:
                    completed = self.runner(
                        command, cwd=workdir, env=process_env, text=True,
                        input=prompt, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=config.timeout_seconds, check=False,
                    )
                except subprocess.TimeoutExpired:
                    raise AgentExecutionError("Codex CLI exceeded its configured timeout") from None
                except OSError:
                    raise AgentExecutionError("Unable to start or stop Codex CLI") from None
                if completed.returncode != 0:
                    raise AgentExecutionError("Codex CLI did not complete successfully")
                response = self._decode_response(self._read_response(output_file))
                try:
                    validator.validate(response)
                except Exception:
                    raise AgentExecutionError("Codex CLI response does not match the configured JSON schema") from None
                return response
        except OSError:
            raise AgentExecutionError("Unable to access Codex CLI control files") from None
