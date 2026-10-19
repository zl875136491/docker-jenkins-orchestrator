import hashlib
from io import BytesIO
import json
from pathlib import Path
import tarfile
import urllib.request

import pytest

from test_jenkins_pipeline import run_stage


def archive_bytes(files):
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as bundle:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            bundle.addfile(member, BytesIO(content))
    return stream.getvalue()


@pytest.mark.parametrize("build_source", ["git", "workspace"])
def test_pipeline_prepares_snapshot_and_builds_with_uploaded_files(tmp_path, monkeypatch, build_source):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo.txt").write_text("git source")
    bundle = archive_bytes({"Dockerfile": b"FROM scratch\nCOPY message.txt /message.txt\n", "message.txt": b"uploaded"})
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: BytesIO(bundle))
    compose = {"x-orchestrator-build-source": build_source, "services": {"web": {"build": {"context": ".", "dockerfile": "Dockerfile"}, "volumes": [".:/workspace"]}}}
    for name, value in {
        "WORKSPACE_ARCHIVE_URL": "https://api.example/snapshot?token=secret",
        "WORKSPACE_ARCHIVE_SHA256": hashlib.sha256(bundle).hexdigest(),
        "COMPOSE_JSON": json.dumps(compose), "APPID": "demo", "BUILD_NUMBER": "1",
        "IMAGE_REPOSITORY": "harbor.example/apps/demo", "HARBOR_REGISTRY": "harbor.example",
    }.items():
        monkeypatch.setenv(name, value)
    run_stage("Prepare Workspace")
    root = tmp_path / ".orchestrator-build-source"
    assert (root / "message.txt").read_bytes() == b"uploaded"
    assert (root / "repo.txt").exists() is (build_source == "git")
    (tmp_path / "input-compose.json").write_text(json.dumps(compose))
    (tmp_path / "input-environment.json").write_text("{}")
    run_stage("Validate Compose")
    plan = json.loads((tmp_path / "build-plan.json").read_text())
    assert plan[0]["context"] == ".orchestrator-build-source"
    assert plan[0]["dockerfile"] == ".orchestrator-build-source/Dockerfile"


@pytest.mark.parametrize("failure", ["checksum", "traversal", "symlink", "repository-symlink"])
def test_pipeline_rejects_unsafe_archives_and_overlay(tmp_path, monkeypatch, failure):
    monkeypatch.chdir(tmp_path)
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as bundle:
        member = tarfile.TarInfo("../escape" if failure == "traversal" else "dir/file")
        if failure == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/tmp"
        bundle.addfile(member)
    data = stream.getvalue()
    if failure == "repository-symlink":
        (tmp_path / "dir").symlink_to(tmp_path.parent)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: BytesIO(data))
    monkeypatch.setenv("WORKSPACE_ARCHIVE_URL", "https://api.example/secret-url")
    monkeypatch.setenv("WORKSPACE_ARCHIVE_SHA256", "bad" if failure == "checksum" else hashlib.sha256(data).hexdigest())
    monkeypatch.setenv("COMPOSE_JSON", "{}")
    with pytest.raises(SystemExit, match="Workspace download or extraction failed"):
        run_stage("Prepare Workspace")


def test_workspace_checkout_does_not_require_git(monkeypatch):
    monkeypatch.setenv("COMPOSE_JSON", json.dumps({"x-orchestrator-build-source": "workspace", "services": {"web": {"build": "."}}}))
    with pytest.raises(SystemExit) as result:
        run_stage("Checkout")
    assert result.value.code == 0
