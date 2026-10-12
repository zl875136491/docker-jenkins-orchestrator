from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.models import UserAppCreate
from orchestrator.naming import (
    APPID_MAX_LENGTH,
    NAMESPACE_PREFIX,
    NamingError,
    bounded_resource_name,
    external_namespace,
    managed_volume_name,
    template_volume_alias,
    validate_appid,
    validate_image_repository,
)


def test_canonical_lowercase_appids_keep_their_external_names() -> None:
    for appid in ("demo", "demo-app", "foo_bar", "foo__bar", "foo--bar"):
        assert external_namespace(appid) == appid


def test_noncanonical_appids_are_case_sensitive_and_collision_free() -> None:
    uppercase = external_namespace("Foo")
    lowercase = external_namespace("foo")
    reserved = external_namespace(f"{NAMESPACE_PREFIX}466f6f")

    assert uppercase == f"{NAMESPACE_PREFIX}466f6f"
    assert lowercase == "foo"
    assert reserved != uppercase
    assert reserved.startswith(NAMESPACE_PREFIX)
    assert external_namespace("Foo") == uppercase
    assert len(external_namespace("A" * APPID_MAX_LENGTH)) == len(NAMESPACE_PREFIX) + APPID_MAX_LENGTH * 2


@pytest.mark.parametrize("appid", ["foo_", "foo-", "foo___bar", "FOO_bar"])
def test_repository_incompatible_but_valid_business_ids_are_encoded(appid: str) -> None:
    assert external_namespace(appid) == NAMESPACE_PREFIX + appid.encode("ascii").hex()


def test_encoding_is_not_idempotent_and_reserved_prefix_cannot_shadow_outputs() -> None:
    encoded = external_namespace("Foo")
    assert external_namespace(encoded) != encoded
    assert external_namespace(encoded) == NAMESPACE_PREFIX + encoded.encode("ascii").hex()


def test_user_app_create_validates_business_id_without_normalizing_it() -> None:
    appid = "APP-20261010-1FDF7D6A"
    request = UserAppCreate(appid=appid, name="Case Sensitive", repository_url="https://git.example/demo.git")
    assert request.appid == appid
    with pytest.raises(ValidationError):
        UserAppCreate(appid="bad.id", name="Invalid", repository_url="https://git.example/demo.git")


@pytest.mark.parametrize("appid", ["", "-bad", "_bad", "bad/part", "bad.id", "bad space", "bad\n", "中文", "a" * (APPID_MAX_LENGTH + 1)])
def test_validate_appid_rejects_non_business_ids(appid: str) -> None:
    with pytest.raises(NamingError):
        validate_appid(appid)


@pytest.mark.parametrize(
    "repository",
    [
        "harbor.example:5000/apps/app",
        "localhost:5000/apps/app",
        "[::1]:5000/apps/app",
        "Harbor.EXAMPLE:5000/apps/app",
        "registry:5000/apps/app",
        "localhost/apps/app",
        "LOCALHOST/apps/app",
        "[::1]/apps/app",
        "a" * 63 + ".example/apps/app",
        "harbor.example/project/a.b_c__d--e",
    ],
)
def test_validate_image_repository_accepts_registry_hosts(repository: str) -> None:
    assert validate_image_repository(repository) == repository


@pytest.mark.parametrize(
    "repository",
    [
        "harbor/apps/app",
        "harbor.example/apps/APP",
        "harbor.example/apps/app_",
        "harbor.example/apps/a___b",
        "harbor.example/apps/app:latest",
        "harbor.example/apps/app@sha256:abc",
        "harbor.example//apps",
        "https://harbor.example/apps/app",
        "harbor.example:bad/apps/app",
        "harbor.example:0/apps/app",
        "harbor.example:65536/apps/app",
        "harbor.example:123456/apps/app",
        "a" * 64 + ".example/apps/app",
        "harbor..example/apps/app",
        "harbor.-example/apps/app",
        "harbor.example-/apps/app",
        "[::::]/apps/app",
        "harbor.example/apps/app?query=1",
        "harbor.example/apps/app#fragment",
        "harbor.example/apps/app/",
        " harbor.example/apps/app",
        "harbor.example/" + "a" * 254 + "/b",
        "harbor.example/" + "a" * 256,
    ],
)
def test_validate_image_repository_rejects_ambiguous_or_tagged_references(repository: str) -> None:
    with pytest.raises(NamingError):
        validate_image_repository(repository)


def test_bounded_resource_name_is_stable_and_collision_resistant() -> None:
    first = bounded_resource_name("x" * 137, "api")
    second = bounded_resource_name("x" * 137, "worker")

    assert len(first) == 63
    assert len(second) == 63
    assert first != second
    assert first == bounded_resource_name("x" * 137, "api")


def test_managed_volume_name_separates_namespace_and_alias_boundaries() -> None:
    assert managed_volume_name("foo", "bar-data") != managed_volume_name("foo-bar", "data")
    assert managed_volume_name("foo", "data") != managed_volume_name("foo", "foo-data")
    assert managed_volume_name(external_namespace("Foo"), "data") != managed_volume_name("foo", "data")
    assert managed_volume_name("foo", "Data") != managed_volume_name("foo", "data")
    assert managed_volume_name("foo", "data") == managed_volume_name("foo", "data")


def test_template_volume_alias_separates_component_and_source() -> None:
    assert template_volume_alias("foo", "bar-data") != template_volume_alias("foo-bar", "data")
    assert template_volume_alias("foo", "Data") != template_volume_alias("foo", "data")
    assert template_volume_alias("foo", "data") == template_volume_alias("foo", "data")
    assert len(template_volume_alias("component" * 64, "source" * 64)) <= 63


@pytest.mark.parametrize("appid", ["foo", "Foo", "appid-v1-466f6f", "A" * APPID_MAX_LENGTH])
def test_managed_volume_name_bounds_readable_names_and_keeps_full_identity_digest(appid: str) -> None:
    namespace = external_namespace(appid)
    name = managed_volume_name(namespace, "service-" + "data" * 64)
    assert len(name) <= 63
    assert name == name.lower()
    assert name.startswith("vol-v1-")
    digest = name.rsplit("-", 1)[1]
    assert len(digest) == 32
    assert all(character in "0123456789abcdef" for character in digest)


@pytest.mark.parametrize(
    ("namespace", "alias"),
    [("Foo", "data"), ("", "data"), ("foo", ""), ("foo", "../data"), ("foo", "data\n")],
)
def test_managed_volume_name_rejects_invalid_external_identity(namespace: str, alias: str) -> None:
    with pytest.raises(NamingError):
        managed_volume_name(namespace, alias)


def test_repository_path_limit_excludes_registry_host() -> None:
    reference = "harbor.example/" + "a" * 255
    assert validate_image_repository(reference) == reference


@pytest.mark.parametrize("max_length", [1, 10, 11, 12, 63])
def test_bounded_resource_name_stays_within_even_tiny_limits(max_length: int) -> None:
    name = bounded_resource_name("a" * 137, "service", max_length=max_length)
    assert 1 <= len(name) <= max_length
    assert name[0].isalnum()


@pytest.mark.parametrize("max_length", [0, -1, True, 1.5])
def test_bounded_resource_name_rejects_invalid_limits(max_length: int) -> None:
    with pytest.raises(NamingError):
        bounded_resource_name("demo", max_length=max_length)
    assert len(bounded_resource_name("x" * 137, "api", max_length=10)) == 10
