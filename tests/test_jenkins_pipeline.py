"""Decode the pipeline's Groovy strings and execute Python without Docker I/O.

The string checks are not a complete Groovy compiler or Jenkins declarative
linter; a real Jenkins validation remains the complete-pipeline gate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest


PIPELINE = Path(__file__).resolve().parents[1] / "jenkins" / "orchestrator-build.groovy"
GROOVY_SINGLE_QUOTED = re.compile(
    r"'''(?:\\(?:\r\n|[\s\S])|(?!''')[^\\])*'''"
    r"|(?!''')'(?:\\(?:\r\n|[\s\S])|[^'\\\r\n])*'"
)
GROOVY_ESCAPE = re.compile(r"\\(u+[0-9A-Fa-f]{4}|[0-3][0-7]{0,2}|[4-7][0-7]?|[\s\S]|$)")


def decode_groovy_single_quoted(literal: str) -> str:
    """Decode the Groovy 2.x single/triple-single literals used by Jenkins."""

    if GROOVY_SINGLE_QUOTED.fullmatch(literal) is None:
        raise ValueError("Invalid or unterminated Groovy single-quoted string")
    width = 3 if literal.startswith("'''") else 1
    body = literal[width:-width].replace("\r\n", "\n").replace("\r", "\n")
    escapes = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", "\\": "\\", "'": "'", '"': '"', "$": "$"}

    def decode_escape(match: re.Match[str]) -> str:
        escaped = match.group(1)
        if escaped in escapes:
            return escapes[escaped]
        if escaped == "\n":
            return ""
        if re.fullmatch(r"u+[0-9A-Fa-f]{4}", escaped):
            return chr(int(escaped[-4:], 16))
        if re.fullmatch(r"[0-7]{1,3}", escaped):
            return chr(int(escaped, 8))
        raise ValueError(f"Invalid Groovy string escape {match.group(0)!r}")

    return GROOVY_ESCAPE.sub(decode_escape, body)


def pipeline_stage(name: str, *, source: str | None = None) -> str:
    if source is None:
        source = PIPELINE.read_text(encoding="utf-8")
    stage = source.split(f"stage('{name}')", 1)[1]
    sh_call = re.search(r"\bsh\s+", stage)
    literal = GROOVY_SINGLE_QUOTED.match(stage, sh_call.end()) if sh_call else None
    if literal is None:
        raise ValueError(f"Stage {name!r} must contain a single-quoted Groovy sh string")
    shell = decode_groovy_single_quoted(literal.group(0))
    script = re.search(r"python3 - <<'PY'\n(.*?)\nPY(?:\n|$)", shell, re.DOTALL)
    if script is None:
        raise ValueError(f"Stage {name!r} must contain an embedded Python heredoc")
    return script.group(1)


def pipeline_cleanup() -> str:
    return pipeline_stage("Cleanup", source="stage('Cleanup') {" + PIPELINE.read_text(encoding="utf-8").split("  post {", 1)[1])


def test_pipeline_uses_build_private_workspace_and_explicit_platform_id():
    source = PIPELINE.read_text(encoding="utf-8")
    assert "customWorkspace" in source and "/builds/" in source
    assert 'PLATFORM_BUILD_ID = "${params.BUILD_ID}"' in source
    assert 'os.environ.get("BUILD_ID"' not in source
    compile(pipeline_cleanup(), str(PIPELINE), "exec")


def test_pipeline_new_artifact_has_platform_identity_and_verified_digests(pipeline_workspace, monkeypatch):
    build_id = "a" * 32
    repository = os.environ["IMAGE_REPOSITORY"]
    monkeypatch.setenv("PLATFORM_BUILD_ID", build_id)
    run_stage("Validate Compose")
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(stdout=json.dumps([{"RepoDigests": [repository + "@sha256:" + "b" * 64]}]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", execute)
    run_stage("Build And Push")
    run_stage("Verify Artifact")
    artifact = json.loads((pipeline_workspace / "orchestrator-result.json").read_text())
    assert artifact["build_id"] == build_id
    assert artifact["images"] == [repository + ":" + build_id + "-api"]
    assert set(artifact["image_digests"].values()) == {"sha256:" + "b" * 64}


@pytest.mark.parametrize("inspection", ["present", "missing", "unavailable"])
def test_pipeline_post_records_precise_image_cleanup_and_removes_private_workspace(tmp_path, monkeypatch, inspection):
    build_id = "a" * 32
    workspace = tmp_path / build_id
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("PLATFORM_BUILD_ID", build_id)
    monkeypatch.setenv("IMAGE_REPOSITORY", "harbor.example/apps/demo")
    reference = "harbor.example/apps/demo:" + build_id + "-web"
    (workspace / "build-plan.json").write_text(json.dumps([{"destination": reference}]))
    (workspace / "checkout").mkdir()
    (workspace / "checkout" / "secret.txt").write_text("secret")
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout='"builder-id"', stderr="")
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0 if inspection == "present" else 1,
                stdout=json.dumps([{"Id": "sha256:" + "b" * 64}]),
                stderr="No such image" if inspection == "missing" else "daemon unavailable")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", execute)
    exec(compile(pipeline_cleanup(), str(PIPELINE), "exec"), {})
    report = json.loads((workspace / "orchestrator-resources.json").read_text())
    assert report["build_id"] == build_id
    assert report["builder_target"] == "docker:builder-id"
    assert report["verification_complete"] is True
    assert report["workspace_removed"] is True
    assert report["images"][0]["local_removed"] is (inspection != "unavailable")
    assert list(workspace.iterdir()) == [workspace / "orchestrator-resources.json"]
    removal = [command for command in calls if command[:3] == ["docker", "image", "rm"]]
    assert removal == ([["docker", "image", "rm", "--no-prune", reference]] if inspection == "present" else [])


def run_stage(name: str) -> None:
    exec(compile(pipeline_stage(name), str(PIPELINE), "exec"), {})


@pytest.mark.parametrize("delimiter", ["'", "'''"])
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("中文 $HOME ${ENV}", "中文 $HOME ${ENV}"),
        (r"\b\t\n\f\r", "\b\t\n\f\r"),
        (r"\'\"\\\$", "'\"\\$"),
        (r"\u20AC\uu0041", "€A"),
        (r"\0\7\10\77\177\377\400\777", "\0\7\10\77\177\377\40" + "0" + "\77" + "7"),
    ],
)
def test_decode_groovy_single_quoted(delimiter: str, body: str, expected: str) -> None:
    assert decode_groovy_single_quoted(delimiter + body + delimiter) == expected


@pytest.mark.parametrize("delimiter", ["'", "'''"])
@pytest.mark.parametrize("escape", [r"\.", r"\[", r"\]", r"\q", r"\x41", r"\8", r"\u12", r"\u12GG"])
def test_decode_groovy_rejects_invalid_escapes(delimiter: str, escape: str) -> None:
    with pytest.raises(ValueError, match="Invalid Groovy string escape"):
        decode_groovy_single_quoted(delimiter + escape + delimiter)


@pytest.mark.parametrize("delimiter", ["'", "'''"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_decode_groovy_line_continuation(delimiter: str, newline: str) -> None:
    assert decode_groovy_single_quoted(delimiter + "one\\" + newline + "two" + delimiter) == "onetwo"


def test_decode_groovy_multiline_and_escaped_triple_quotes() -> None:
    assert decode_groovy_single_quoted("'''one\r\ntwo\rthree'''") == "one\ntwo\nthree"
    assert decode_groovy_single_quoted("'''one\\'''two'''") == "one'''two"
    with pytest.raises(ValueError, match="Invalid or unterminated"):
        decode_groovy_single_quoted("'one\ntwo'")


@pytest.mark.parametrize("name", ["Checkout", "Prepare Workspace", "Validate Compose", "Build And Push", "Verify Artifact"])
def test_pipeline_shell_strings_decode_before_python_compilation(name: str) -> None:
    compile(pipeline_stage(name), str(PIPELINE), "exec")


@pytest.mark.parametrize("escape", [r"\.", r"\[", r"\]"])
def test_pipeline_rejects_regression_to_single_backslash_registry_escapes(escape: str) -> None:
    source = PIPELINE.read_text(encoding="utf-8")
    doubled = escape.replace("\\", "\\\\")
    assert doubled in source
    source = source.replace(doubled, escape, 1)
    with pytest.raises(ValueError, match="Invalid Groovy string escape"):
        pipeline_stage("Validate Compose", source=source)


def test_pipeline_registry_regex_reaches_python_with_one_backslash() -> None:
    assert r"(?:\.{host_component})*|\[[A-Fa-f0-9:]+\]" in pipeline_stage("Validate Compose")


def test_pipeline_rejects_invalid_escape_outside_embedded_python() -> None:
    invalid = r"echo '\q'"
    source = f"stage('Example') {{ sh '''{invalid}\npython3 - <<'PY'\npass\nPY\n''' }}"
    with pytest.raises(ValueError, match="Invalid Groovy string escape"):
        pipeline_stage("Example", source=source)


@pytest.fixture
def pipeline_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "APPID": "APP-20261010-1FDF7D6A",
        "IMAGE_REPOSITORY": "harbor.example:5000/apps/appid-v1-4150502d32303236313031302d3146444637443641",
        "HARBOR_REGISTRY": "harbor.example:5000",
        "BUILD_NUMBER": "45",
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "input-compose.json").write_text(
        json.dumps({"services": {"api": {"build": {"context": ".", "dockerfile": "Dockerfile"}, "image": "example/api:1", "ports": ["18088:80"]}}}),
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (tmp_path / "input-environment.json").write_text("{}", encoding="utf-8")
    (tmp_path / "checkout.json").write_text('{"actual_commit":null,"image_only":true}', encoding="utf-8")
    return tmp_path


def test_pipeline_accepts_raw_uppercase_appid_and_safe_repository_with_port(pipeline_workspace: Path) -> None:
    run_stage("Validate Compose")
    plan = json.loads((pipeline_workspace / "build-plan.json").read_text())
    assert plan[0]["destination"] == (
        "harbor.example:5000/apps/appid-v1-4150502d32303236313031302d3146444637443641:45-api"
    )


@pytest.mark.parametrize(
    "repository",
    [
        "harbor.example/apps/APP-20261010-1FDF7D6A",
        "harbor.example/apps/app_",
        "harbor.example/apps/a___b",
        "harbor.example/apps/app:latest",
        "harbor.example/apps/app@sha256:abc",
        "harbor.example//app",
        "https://harbor.example/apps/app",
        "harbor/apps/app",
        "harbor.example:bad/apps/app",
        "harbor.example:0/apps/app",
        "harbor.example:65536/apps/app",
        "harbor.example:123456/apps/app",
        "harbor..example/apps/app",
        "harbor.-example/apps/app",
        "[::::]/apps/app",
        "harbor.example/apps/app?query=1",
        "harbor.example/apps/app#fragment",
        "a" * 64 + ".example/apps/app",
        "harbor.example/" + "a" * 256,
    ],
)
def test_pipeline_rejects_invalid_repositories_before_build_plan(
    pipeline_workspace: Path, monkeypatch: pytest.MonkeyPatch, repository: str
) -> None:
    monkeypatch.setenv("IMAGE_REPOSITORY", repository)
    monkeypatch.setenv("HARBOR_REGISTRY", repository.split("/", 1)[0])
    with pytest.raises(SystemExit, match="IMAGE_REPOSITORY"):
        run_stage("Validate Compose")
    assert not (pipeline_workspace / "build-plan.json").exists()


@pytest.mark.parametrize(
    "registry",
    ["localhost:5000", "localhost", "LOCALHOST", "[::1]:5000", "[::1]", "Harbor.EXAMPLE:5000", "registry:65535", "a" * 63 + ".example"],
)
def test_pipeline_accepts_docker_registry_hosts(
    pipeline_workspace: Path, monkeypatch: pytest.MonkeyPatch, registry: str
) -> None:
    monkeypatch.setenv("IMAGE_REPOSITORY", f"{registry}/apps/app")
    monkeypatch.setenv("HARBOR_REGISTRY", registry)
    run_stage("Validate Compose")


def test_pipeline_repository_path_limit_excludes_registry_host(
    pipeline_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_REPOSITORY", "harbor.example/" + "a" * 255)
    monkeypatch.setenv("HARBOR_REGISTRY", "harbor.example")
    run_stage("Validate Compose")


def test_pipeline_rejects_login_registry_mismatch(pipeline_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBOR_REGISTRY", "other.example")
    with pytest.raises(SystemExit, match="HARBOR_REGISTRY"):
        run_stage("Validate Compose")


def test_pipeline_rejects_overlong_image_tag_before_any_docker_call(pipeline_workspace: Path) -> None:
    (pipeline_workspace / "input-compose.json").write_text(
        json.dumps({"services": {"a" * 126: {"image": "example/api:1"}}}), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="128-character"):
        run_stage("Validate Compose")
    assert not (pipeline_workspace / "build-plan.json").exists()


def test_pipeline_builds_validated_destinations_and_preserves_volume_metadata(
    pipeline_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose: dict[str, Any] = {
        "services": {
            "API": {"build": {"context": ".", "dockerfile": "Dockerfile"}, "image": "example/api:1", "ports": ["18088:80"], "volumes": ["data:/data"]},
            "web": {"build": {"context": ".", "dockerfile": "Dockerfile"}, "ports": ["18089:8080"]},
        },
        "volumes": {"data": {"external": True, "name": "shared-data"}},
    }
    (pipeline_workspace / "input-compose.json").write_text(json.dumps(compose), encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))
    run_stage("Validate Compose")
    run_stage("Build And Push")
    run_stage("Verify Artifact")

    artifact = json.loads((pipeline_workspace / "orchestrator-result.json").read_text())
    assert len(artifact["images"]) == 2
    assert set(artifact["compose"]["services"]) == {"API", "web"}
    assert artifact["compose"]["volumes"] == compose["volumes"]
    assert artifact["compose"]["services"]["API"]["ports"] == ["18088:80"]
    assert artifact["compose"]["services"]["API"]["image"].endswith(":45-API")
    assert "build" not in artifact["compose"]["services"]["web"]
    assert calls[0][:2] == ["docker", "build"]
    assert [command[1] for command in calls] == ["build", "push", "build", "push"]
    assert {command[-1] for command in calls if command[1] == "push"} == set(artifact["images"])


def test_pipeline_inherits_only_node_proxy_settings_into_private_docker_config(
    pipeline_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_config = pipeline_workspace / "host-docker.json"
    proxies = {"default": {"httpProxy": "http://proxy.example:7890", "httpsProxy": "http://proxy.example:7890", "noProxy": "harbor.example,localhost"}}
    host_settings = {"auths": {"host.example": {"auth": "host-auth-not-for-build"}}, "credsStore": "host-helper", "proxies": proxies}
    host_config.write_text(json.dumps(host_settings), encoding="utf-8")
    private_directory = pipeline_workspace / "private-docker"
    private_directory.mkdir()
    private_config = private_directory / "config.json"
    private_settings = {"auths": {"harbor.example": {"auth": "private-build-auth"}}}
    private_config.write_text(json.dumps(private_settings), encoding="utf-8")
    monkeypatch.setenv("ORCHESTRATOR_DOCKER_PROXY_CONFIG", str(host_config))
    monkeypatch.setenv("DOCKER_CONFIG", str(private_directory))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))
    run_stage("Validate Compose")
    run_stage("Build And Push")
    assert json.loads(private_config.read_text()) == {**private_settings, "proxies": proxies}
    assert json.loads(host_config.read_text()) == host_settings
    assert private_config.stat().st_mode & 0o777 == 0o600
    assert calls


@pytest.mark.parametrize("host_settings", [None, {}, {"auths": {"host.example": {"auth": "host-auth-not-for-build"}}}])
def test_pipeline_preserves_private_docker_credentials_without_node_proxies(
    pipeline_workspace: Path, monkeypatch: pytest.MonkeyPatch, host_settings: dict | None
) -> None:
    host_config = pipeline_workspace / "host-docker.json"
    if host_settings is not None:
        host_config.write_text(json.dumps(host_settings), encoding="utf-8")
    private_directory = pipeline_workspace / "private-docker"
    private_directory.mkdir()
    private_config = private_directory / "config.json"
    private_settings = {"auths": {"harbor.example": {"auth": "private-build-auth"}}}
    private_config.write_text(json.dumps(private_settings), encoding="utf-8")
    monkeypatch.setenv("ORCHESTRATOR_DOCKER_PROXY_CONFIG", str(host_config))
    monkeypatch.setenv("DOCKER_CONFIG", str(private_directory))
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: None)
    run_stage("Validate Compose")
    run_stage("Build And Push")
    assert json.loads(private_config.read_text()) == private_settings


def test_pipeline_rejects_invalid_node_proxies_before_docker_calls(
    pipeline_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_config = pipeline_workspace / "host-docker.json"
    host_config.write_text(json.dumps({"proxies": "invalid"}), encoding="utf-8")
    private_directory = pipeline_workspace / "private-docker"
    private_directory.mkdir()
    monkeypatch.setenv("ORCHESTRATOR_DOCKER_PROXY_CONFIG", str(host_config))
    monkeypatch.setenv("DOCKER_CONFIG", str(private_directory))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))
    run_stage("Validate Compose")
    with pytest.raises(ValueError, match="proxies configuration must be an object"):
        run_stage("Build And Push")
    assert calls == []


@pytest.mark.parametrize("change", ["declarations", "mounts"])
def test_pipeline_verification_rejects_changed_volume_contract(
    pipeline_workspace: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    compose: dict[str, Any] = {
        "services": {"api": {"build": {"context": ".", "dockerfile": "Dockerfile"}, "image": "example/api:1", "volumes": ["data:/data"]}},
        "volumes": {"data": {"external": True, "name": "shared-data"}},
    }
    (pipeline_workspace / "input-compose.json").write_text(json.dumps(compose), encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    run_stage("Validate Compose")
    run_stage("Build And Push")
    artifact_path = pipeline_workspace / "orchestrator-result.json"
    artifact = json.loads(artifact_path.read_text())
    if change == "declarations":
        artifact["compose"]["volumes"]["data"] = {}
    else:
        artifact["compose"]["services"]["api"]["volumes"] = ["other-data:/data"]
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(AssertionError):
        run_stage("Verify Artifact")
