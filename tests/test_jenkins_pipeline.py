"""Execute the pipeline's embedded Python without Jenkins or Docker I/O."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any

import pytest


PIPELINE = Path(__file__).resolve().parents[1] / "jenkins" / "orchestrator-build.groovy"


def pipeline_stage(name: str) -> str:
    source = PIPELINE.read_text(encoding="utf-8")
    stage = source.split(f"stage('{name}')", 1)[1]
    script = re.search(r"python3 - <<'PY'\n(.*?)\nPY", stage, re.DOTALL).group(1)
    # The Groovy shell string escapes a Python ``\\n`` once before Jenkins
    # invokes the shell.  Normalize it for direct offline execution here.
    return script.replace('\\\\n', '\\n')


def run_stage(name: str) -> None:
    exec(compile(pipeline_stage(name), str(PIPELINE), "exec"), {})


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
        json.dumps({"services": {"api": {"image": "example/api:1", "ports": ["18088:80"]}}}),
        encoding="utf-8",
    )
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
            "API": {"image": "example/api:1", "ports": ["18088:80"], "volumes": ["data:/data"]},
            "web": {"build": ".", "ports": ["18089:8080"]},
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
    assert calls[0] == ["docker", "pull", "example/api:1"]
    assert [command[1] for command in calls] == ["pull", "tag", "push", "build", "push"]
    assert {command[-1] for command in calls if command[1] == "push"} == set(artifact["images"])


@pytest.mark.parametrize("change", ["declarations", "mounts"])
def test_pipeline_verification_rejects_changed_volume_contract(
    pipeline_workspace: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    compose: dict[str, Any] = {
        "services": {"api": {"image": "example/api:1", "volumes": ["data:/data"]}},
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
