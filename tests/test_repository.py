from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.models import BaseImage, BuildJob, BuildStatus
from orchestrator.repository import InMemoryRepository


def test_base_image_storage_is_separate_from_user_apps() -> None:
    repository = InMemoryRepository()
    image = BaseImage(source_image="python:3.12", harbor_reference="boot-images/python")
    repository.save_base_image(image)
    assert repository.base_images["python:3.12"].harbor_reference == "boot-images/python"
    assert repository.apps == {}


def test_build_history_filters_sorts_and_pages() -> None:
    repository = InMemoryRepository()
    start = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    builds = (
        BuildJob(build_id="old", appid="alpha", git_ref="main", created_at=start),
        BuildJob(
            build_id="tie-a",
            appid="alpha",
            git_ref="main",
            status=BuildStatus.QUEUED,
            created_at=start + timedelta(minutes=1),
        ),
        BuildJob(
            build_id="tie-z",
            appid="alpha",
            git_ref="main",
            status=BuildStatus.FAILED,
            created_at=start + timedelta(minutes=1),
        ),
        BuildJob(
            build_id="new",
            appid="beta",
            git_ref="main",
            status=BuildStatus.SUCCEEDED,
            created_at=start + timedelta(minutes=2),
        ),
    )
    for build in builds:
        repository.create_build(build)

    all_builds, total = repository.list_builds()
    assert total == 4
    assert [build.build_id for build in all_builds] == ["new", "tie-z", "tie-a", "old"]

    queued_alpha, queued_total = repository.list_builds(appid="alpha", status=BuildStatus.QUEUED)
    assert queued_total == 2
    assert [build.build_id for build in queued_alpha] == ["tie-a", "old"]

    page, page_total = repository.list_builds(skip=1, limit=2)
    assert page_total == 4
    assert [build.build_id for build in page] == ["tie-z", "tie-a"]


def test_build_history_rejects_invalid_repository_pagination() -> None:
    repository = InMemoryRepository()
    with pytest.raises(ValueError, match="skip"):
        repository.list_builds(skip=-1)
    with pytest.raises(ValueError, match="limit"):
        repository.list_builds(limit=0)
