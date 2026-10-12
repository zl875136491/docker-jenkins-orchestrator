from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from orchestrator.models import BaseImage, BuildJob, BuildStatus, UserAppRecord
from orchestrator.repository import InMemoryRepository, MongoRepository


def _app_record(appid: str = "Foo") -> UserAppRecord:
    return UserAppRecord(
        appid=appid,
        name=f"app {appid}",
        repository_url=f"https://git.example/{appid}.git",
        git_ref="main",
        environment_ciphertext="{}",
    )


class FakeAppCollection:
    def __init__(self, app: UserAppRecord) -> None:
        self.document = app.model_dump(mode="python")
        self.updates: list[tuple[dict[str, Any], dict[str, Any], Any]] = []

    def find_one_and_update(self, query, update, *, return_document):
        self.updates.append((query, update, return_document))
        if self.document["appid"] != query["appid"]:
            return None
        self.document.update(update["$set"])
        return dict(self.document)


@pytest.fixture
def mongo_app_repository() -> tuple[MongoRepository, FakeAppCollection]:
    collection = FakeAppCollection(_app_record())
    database = {
        name: object()
        for name in (
            "build_jobs",
            "app_events",
            "user_images",
            "base_images",
            "tech_stacks",
            "deployment_services",
            "alerts",
        )
    }
    database["user_apps"] = collection
    return MongoRepository(database), collection


def test_base_image_storage_is_separate_from_user_apps() -> None:
    repository = InMemoryRepository()
    image = BaseImage(source_image="python:3.12", harbor_reference="boot-images/python")
    repository.save_base_image(image)
    assert repository.base_images["python:3.12"].harbor_reference == "boot-images/python"
    assert repository.apps == {}


def test_app_identity_and_build_filtering_preserve_appid_case() -> None:
    repository = InMemoryRepository()
    for appid in ("Foo", "foo"):
        repository.create_app(
            UserAppRecord(
                appid=appid,
                name=f"app {appid}",
                repository_url=f"https://git.example/{appid}.git",
                git_ref="main",
                environment_ciphertext="{}",
            )
        )
        repository.create_build(BuildJob(build_id=f"build-{appid}", appid=appid, git_ref="main"))

    assert repository.get_app("Foo").appid == "Foo"
    assert repository.get_app("foo").appid == "foo"
    assert repository.get_app("FOO") is None
    assert repository.list_builds(appid="Foo")[1] == 1
    assert repository.list_builds(appid="foo")[1] == 1


@pytest.mark.parametrize("replacement_appid", ["foo", "other", None])
def test_in_memory_app_updates_reject_identity_changes(replacement_appid: str | None) -> None:
    repository = InMemoryRepository()
    original = repository.create_app(_app_record())
    repository.create_build(BuildJob(build_id="build-Foo", appid="Foo", git_ref="main"))
    fields = {"appid": replacement_appid, "name": "renamed"}

    with pytest.raises(ValueError, match="appid is immutable"):
        repository.update_app("Foo", fields)

    assert repository.get_app("Foo") == original
    assert repository.get_app("foo") is None
    assert repository.get_app("other") is None
    assert repository.list_builds(appid="Foo")[1] == 1
    assert fields == {"appid": replacement_appid, "name": "renamed"}


def test_in_memory_app_updates_allow_same_identity_without_changing_foreign_keys() -> None:
    repository = InMemoryRepository()
    repository.create_app(_app_record())
    repository.create_build(BuildJob(build_id="build-Foo", appid="Foo", git_ref="main"))
    fields = {"appid": "Foo", "name": "renamed"}

    updated = repository.update_app("Foo", fields)

    assert updated.appid == "Foo"
    assert updated.name == "renamed"
    assert repository.get_app("Foo") == updated
    assert repository.get_app("foo") is None
    builds, total = repository.list_builds(appid="Foo")
    assert total == 1
    assert builds[0].appid == "Foo"
    assert repository.list_builds(appid="foo")[1] == 0
    assert fields == {"appid": "Foo", "name": "renamed"}


@pytest.mark.parametrize("replacement_appid", ["foo", "other", None])
def test_mongo_app_updates_reject_identity_changes_before_writing(
    mongo_app_repository: tuple[MongoRepository, FakeAppCollection], replacement_appid: str | None
) -> None:
    repository, collection = mongo_app_repository
    original = dict(collection.document)
    fields = {"appid": replacement_appid, "name": "renamed"}

    with pytest.raises(ValueError, match="appid is immutable"):
        repository.update_app("Foo", fields)

    assert collection.updates == []
    assert collection.document == original
    assert fields == {"appid": replacement_appid, "name": "renamed"}


def test_mongo_app_updates_remove_same_identity_from_set_without_changing_caller_fields(
    mongo_app_repository: tuple[MongoRepository, FakeAppCollection],
) -> None:
    from pymongo import ReturnDocument

    repository, collection = mongo_app_repository
    fields = {"appid": "Foo", "name": "renamed"}

    updated = repository.update_app("Foo", fields)

    assert updated.appid == "Foo"
    assert updated.name == "renamed"
    assert collection.updates == [({"appid": "Foo"}, {"$set": {"name": "renamed"}}, ReturnDocument.AFTER)]
    assert collection.document["appid"] == "Foo"
    assert fields == {"appid": "Foo", "name": "renamed"}


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
