from pathlib import Path

import pytest
import yaml

from orchestrator.naming import bounded_resource_name, external_namespace, managed_volume_name, template_volume_alias
from orchestrator.templates import TemplateCatalog, TemplateError


CATALOG_PATH = Path(__file__).parents[1] / "templates" / "catalog.yaml"


@pytest.fixture
def catalog() -> TemplateCatalog:
    return TemplateCatalog(CATALOG_PATH)


def test_catalog_covers_requested_components_with_pinned_images(catalog: TemplateCatalog) -> None:
    expected = {
        "python",
        "go",
        "java",
        "cpp",
        "react",
        "vue",
        "nextjs",
        "html",
        "mongodb",
        "mysql",
        "postgresql",
        "redis",
        "minio",
        "etcd",
    }

    assert expected.issubset(catalog.names())
    for name in expected:
        images = catalog.components[name]["images"]
        assert 2 <= len(images) <= 3
        assert all(":latest" not in image for image in images)
        assert catalog.components[name]["build"] == {"context": ".", "dockerfile": "Dockerfile"}


def test_compose_is_deterministic_namespaced_and_injects_internal_hosts(catalog: TemplateCatalog) -> None:
    kwargs = {
        "appid": "demo-app",
        "environment": {"APP_ENV": "production"},
        "service_environment": {"python": {"LOG_LEVEL": "info"}},
        "dependency_conditions": True,
    }
    first = catalog.compose(
        ["react", "mongodb", "python"],
        {"react": ["python"], "python": ["mongodb"]},
        **kwargs,
    )
    second = catalog.compose(
        ["python", "react", "mongodb"],
        {"python": ["mongodb"], "react": ["python"]},
        **kwargs,
    )

    assert first == second
    assert first["name"] == "demo-app"
    assert list(first["services"]) == ["demo-app-mongodb", "demo-app-python", "demo-app-react"]
    assert first["services"]["demo-app-python"]["environment"] == {
        "APP_ENV": "production",
        "LOG_LEVEL": "info",
        "MONGODB_HOST": "demo-app-mongodb",
        "MONGODB_PORT": "27017",
        "MONGODB_URI": "mongodb://demo-app-mongodb:27017",
        "PORT": "8000",
        "PYTHONUNBUFFERED": "1",
    }
    assert first["services"]["demo-app-react"]["environment"]["PYTHON_HOST"] == "demo-app-python"
    assert first["services"]["demo-app-python"]["depends_on"] == {
        "demo-app-mongodb": {"condition": "service_healthy"}
    }
    assert first["services"]["demo-app-react"]["depends_on"] == {
        "demo-app-python": {"condition": "service_started"}
    }
    assert first["services"]["demo-app-mongodb"]["healthcheck"]
    volume_name = managed_volume_name("demo-app", template_volume_alias("mongodb", "data"))
    assert first["volumes"][volume_name] == {"name": volume_name}
    assert first["services"]["demo-app-mongodb"]["volumes"] == [f"{volume_name}:/data/db"]


def test_legacy_dependencies_remain_list_form(catalog: TemplateCatalog) -> None:
    document = catalog.compose(["python", "mongodb"], {"python": ["mongodb"]})

    assert document["services"]["python"]["depends_on"] == ["mongodb"]
    assert document["services"]["python"]["environment"]["MONGODB_HOST"] == "mongodb"


def test_minimal_legacy_catalog_entries_keep_their_published_port(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        "components:\n  example:\n    images: [example:1.0]\n    port: 8080\n",
        encoding="utf-8",
    )

    document = TemplateCatalog(catalog_path).compose(["example"])

    assert document["services"]["example"]["ports"] == ["8080:8080"]
    assert document["services"]["example"]["build"] == {"context": ".", "dockerfile": "Dockerfile"}


def test_common_stack_aliases_resolve_to_canonical_service_names(catalog: TemplateCatalog) -> None:
    document = catalog.compose(["C++", "react.js", "html+css+js"])

    assert list(document["services"]) == ["cpp", "html", "react"]


def test_explicit_dependency_conditions_are_validated(catalog: TemplateCatalog) -> None:
    document = catalog.compose(
        ["python", "mongodb"],
        {"python": {"mongodb": "service_healthy"}},
    )
    assert document["services"]["python"]["depends_on"] == {
        "mongodb": {"condition": "service_healthy"}
    }

    with pytest.raises(TemplateError, match="has no healthcheck"):
        catalog.compose(["python", "react"], {"react": {"python": "service_healthy"}})


def test_compose_yaml_round_trips_and_rejects_invalid_references(catalog: TemplateCatalog) -> None:
    document = catalog.compose(["go", "redis"], {"go": ["redis"]}, appid="api")
    rendered = catalog.compose_yaml(["go", "redis"], {"go": ["redis"]}, appid="api")

    assert yaml.safe_load(rendered) == document
    assert catalog.parse_compose_yaml(rendered) == document

    with pytest.raises(TemplateError, match="selected components"):
        catalog.compose(["go"], {"go": ["redis"]})
    with pytest.raises(TemplateError, match="appid"):
        catalog.compose(["go"], appid="not/a-valid-appid")
    with pytest.raises(TemplateError, match="selected components"):
        catalog.compose(["go"], service_environment={"redis": {"REDIS_URL": "redis://remote"}})
    with pytest.raises(TemplateError, match="Duplicate dependencies"):
        catalog.compose(["go", "redis"], {"go": ["redis", "redis"]})


def test_external_namespace_preserves_case_separates_collisions_and_bounds_long_names(catalog: TemplateCatalog) -> None:
    upper = catalog.compose(["go"], appid="Foo")
    lower = catalog.compose(["go"], appid="foo")
    assert upper["name"] != lower["name"]
    assert list(upper["services"]) != list(lower["services"])
    assert upper["name"].startswith("appid-v1-")
    assert all(len(name) <= 63 for name in upper["services"])

    long_appid = "A" * 64
    document = catalog.compose(["go"], appid=long_appid)
    assert all(len(name) <= 63 for name in document["services"])
    assert all(len(name) <= 63 for name in document.get("volumes", {}))


def test_unbound_template_volumes_remain_implicit_for_application_isolation(catalog: TemplateCatalog) -> None:
    document = catalog.compose(["mongodb"])
    alias = template_volume_alias("mongodb", "data")
    assert document["volumes"] == {alias: {}}
    assert document["services"]["mongodb"]["volumes"] == [f"{alias}:/data/db"]


def test_bound_template_volumes_use_complete_app_namespace(catalog: TemplateCatalog) -> None:
    first = catalog.compose(["mongodb"], appid="foo")
    second = catalog.compose(["mongodb"], appid="foo-bar")
    assert set(first["volumes"]).isdisjoint(second["volumes"])
    for document in (first, second):
        for alias, definition in document["volumes"].items():
            assert definition == {"name": alias}


def test_template_volume_component_source_boundaries_do_not_collapse(catalog: TemplateCatalog) -> None:
    catalog.upsert_component(
        "foo", {"images": ["example/api:1"], "port": 8080, "volumes": [{"source": "bar-data", "target": "/data"}]}
    )
    catalog.upsert_component(
        "foo-bar", {"images": ["example/api:1"], "port": 8080, "volumes": [{"source": "data", "target": "/data"}]}
    )
    for appid in (None, "app", "A" * 64):
        document = catalog.compose(["foo", "foo-bar"], appid=appid)
        assert len(document["volumes"]) == 2
        mounts = [service["volumes"][0] for service in document["services"].values()]
        assert mounts[0] != mounts[1]


def test_bound_and_unbound_template_volume_identity_is_consistent(catalog: TemplateCatalog) -> None:
    generic = catalog.compose(["mongodb"])
    generic_alias = next(iter(generic["volumes"]))
    namespace = external_namespace("Foo")
    app_bound = catalog.compose(["mongodb"], appid="Foo")
    assert set(app_bound["volumes"]) == {managed_volume_name(namespace, generic_alias)}


@pytest.mark.parametrize("source", ["../data", "/data", "data space", "data\n"])
def test_template_rejects_invalid_named_volume_before_persisting_component(
    catalog: TemplateCatalog, source: str
) -> None:
    with pytest.raises(TemplateError, match="valid named source"):
        catalog.upsert_component(
            "example", {"images": ["example/api:1"], "port": 8080, "volumes": [{"source": source, "target": "/data"}]}
        )
    assert "example" not in catalog.components


@pytest.mark.parametrize("appid", ["Foo", "appid-v1-466f6f", "A" * 64, None])
def test_environment_placeholders_keep_business_id_and_external_names_separate(
    catalog: TemplateCatalog, appid: str | None
) -> None:
    catalog.components["go"]["environment"].update(
        {
            "BUSINESS_APPID": "{appid}",
            "EXTERNAL_NAMESPACE": "{namespace}",
            "SERVICE_HOST": "{host}",
            "TEMPLATE_CONTEXT": "{appid}|{namespace}|{host}|{component}|{port}",
        }
    )
    catalog.components["redis"]["connection"]["environment"].update(
        {
            "DEPENDENCY_APPID": "{appid}",
            "DEPENDENCY_NAMESPACE": "{namespace}",
            "DEPENDENCY_CONTEXT": "{appid}|{namespace}|{host}|{component}|{port}",
        }
    )
    namespace = external_namespace(appid) if appid is not None else ""
    go_host = bounded_resource_name(namespace, "go") if namespace else "go"
    redis_host = bounded_resource_name(namespace, "redis") if namespace else "redis"

    document = catalog.compose(["go", "redis"], {"go": ["redis"]}, appid=appid)
    environment = document["services"][go_host]["environment"]

    assert environment["BUSINESS_APPID"] == (appid or "")
    assert environment["EXTERNAL_NAMESPACE"] == namespace
    assert environment["SERVICE_HOST"] == go_host
    assert environment["TEMPLATE_CONTEXT"] == f"{appid or ''}|{namespace}|{go_host}|go|8080"
    assert environment["DEPENDENCY_APPID"] == (appid or "")
    assert environment["DEPENDENCY_NAMESPACE"] == namespace
    assert environment["DEPENDENCY_CONTEXT"] == f"{appid or ''}|{namespace}|{redis_host}|redis|6379"
    assert environment["REDIS_HOST"] == redis_host
    assert environment["REDIS_URL"] == f"redis://{redis_host}:6379/0"
    assert document["services"][go_host]["depends_on"] == [redis_host]
    if appid is None:
        assert "name" not in document
        assert all("labels" not in service for service in document["services"].values())
    else:
        assert document["name"] == bounded_resource_name(namespace)
        assert document["services"][go_host]["labels"]["io.docker-jenkins-orchestrator.appid"] == appid
        assert document["services"][go_host]["labels"]["io.docker-jenkins-orchestrator.namespace"] == namespace
    if appid == "A" * 64:
        assert len(namespace) > 63
        assert len(go_host) <= 63
        assert environment["EXTERNAL_NAMESPACE"] != document["name"]


@pytest.mark.parametrize("appid", ["Foo", "appid-v1-466f6f", "A" * 64, None])
def test_builtin_dependency_hosts_use_final_service_names(catalog: TemplateCatalog, appid: str | None) -> None:
    namespace = external_namespace(appid) if appid is not None else ""
    document = catalog.compose(["python", "mongodb"], {"python": ["mongodb"]}, appid=appid)
    python_host = bounded_resource_name(namespace, "python") if namespace else "python"
    mongodb_host = bounded_resource_name(namespace, "mongodb") if namespace else "mongodb"

    assert document["services"][python_host]["environment"]["MONGODB_HOST"] == mongodb_host
    assert document["services"][python_host]["environment"]["MONGODB_URI"] == f"mongodb://{mongodb_host}:27017"
    assert document["services"][python_host]["depends_on"] == [mongodb_host]
    assert all(len(name) <= 63 for name in document["services"])
    assert all(len(name) <= 63 for name in document["volumes"])


def test_commented_compose_yaml_round_trips_for_every_catalog_component(catalog: TemplateCatalog) -> None:
    for component_name in catalog.names():
        document = catalog.compose([component_name])
        rendered = catalog.render_commented_yaml(document)

        assert yaml.safe_load(rendered) == document
        assert "# " in rendered


def test_commented_data_handles_numeric_scalars_before_sibling_fields(catalog: TemplateCatalog) -> None:
    document = {
        "healthcheck": {
            "retries": 10,
            "start_period": "30s",
            "enabled": True,
        }
    }
    comments = {
        "$": "document help",
        "$.healthcheck": "healthcheck help",
        "$.healthcheck.retries": "retry help",
        "$.healthcheck.start_period": "start period help",
        "$.healthcheck.enabled": "enabled help",
    }

    rendered = catalog.render_commented_data(document, comments)

    assert yaml.safe_load(rendered) == document
    assert "\n...\n" not in rendered
    assert "# retry help\n  retries: 10\n  # start period help" in rendered
