from pathlib import Path

import pytest

from orchestrator.compose_contract import (
    ComposeBuildContractError,
    validate_local_build_compose,
    validate_local_build_files,
)


def compose_with_build(build: object) -> dict:
    return {"services": {"app": {"build": build}}}


def test_local_build_requires_build_mapping() -> None:
    with pytest.raises(ComposeBuildContractError, match="must use local build mode"):
        validate_local_build_compose(compose_with_build("."))


def test_local_build_requires_dockerfile() -> None:
    with pytest.raises(ComposeBuildContractError, match="build.dockerfile is required"):
        validate_local_build_compose(compose_with_build({"context": "."}))


@pytest.mark.parametrize(
    "build",
    [
        {"context": "..", "dockerfile": "Dockerfile"},
        {"context": ".", "dockerfile": "../Dockerfile"},
        {"context": "/workspace", "dockerfile": "Dockerfile"},
        {"context": ".", "dockerfile": "/workspace/Dockerfile"},
    ],
)
def test_local_build_rejects_unsafe_paths(build: dict) -> None:
    with pytest.raises(ComposeBuildContractError, match="safe relative POSIX path"):
        validate_local_build_compose(compose_with_build(build))


def test_local_build_accepts_explicit_context_and_dockerfile() -> None:
    validate_local_build_compose(compose_with_build({"context": ".", "dockerfile": "Dockerfile"}))


def test_local_build_files_requires_context_and_dockerfile_to_exist(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    compose = compose_with_build({"context": ".", "dockerfile": "Dockerfile"})

    validate_local_build_files(compose, source_root)

    (source_root / "Dockerfile").unlink()
    with pytest.raises(ComposeBuildContractError, match="Dockerfile does not exist"):
        validate_local_build_files(compose, source_root)
