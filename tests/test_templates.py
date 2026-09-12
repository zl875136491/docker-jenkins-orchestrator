from pathlib import Path

import pytest
import yaml

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
    assert "demo-app-mongodb-data" in first["volumes"]


def test_legacy_dependencies_remain_list_form(catalog: TemplateCatalog) -> None:
    document = catalog.compose(["python", "mongodb"], {"python": ["mongodb"]})

    assert document["services"]["python"]["depends_on"] == ["mongodb"]
    assert document["services"]["python"]["environment"]["MONGODB_HOST"] == "mongodb"


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
