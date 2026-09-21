from uuid import uuid4

from fastapi.testclient import TestClient

from main import create_app
from main import app
from orchestrator.config import Settings
from orchestrator.models import DeploymentService, PublishedPort


client = TestClient(app)


def test_healthz_smoke() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_cors_allows_any_origin() -> None:
    response = client.get("/healthz", headers={"Origin": "https://any.example"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_cors_preflight_allows_any_method_and_header() -> None:
    response = client.options(
        "/api/apps",
        headers={
            "Origin": "https://any.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_connect_and_protected_route_smoke() -> None:
    response = client.post("/api/connect", json={"worker_name": "local-worker", "worker_secret": "local-worker-secret"})
    assert response.status_code == 200
    token = response.json()["access_token"]

    protected = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert protected.status_code == 200
    assert protected.json() == {"worker_name": "local-worker", "scope": "conductor"}


def test_protected_route_rejects_missing_token() -> None:
    assert client.get("/api/me").status_code == 401


def auth_headers() -> dict[str, str]:
    token = client.post("/api/connect", json={"worker_name": "local-worker", "worker_secret": "local-worker-secret"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_app_build_lifecycle_smoke() -> None:
    headers = auth_headers()
    app_response = client.post(
        "/api/apps",
        headers=headers,
        json={
            "appid": "demo-app",
            "name": "Demo",
            "repository_url": "https://git.example/demo.git",
            "environment": {"DATABASE_URL": "mongodb://mongodb/demo"},
            "compose": {"services": {"api": {"image": "example/demo:latest"}}},
        },
    )
    assert app_response.status_code == 201
    assert client.get("/api/apps/demo-app", headers=headers).status_code == 200

    build_response = client.post("/api/apps/demo-app/builds", headers=headers, json={"git_ref": "main"})
    assert build_response.status_code == 202
    build = build_response.json()
    assert build["status"] == "queued"
    assert client.get(f"/api/builds/{build['build_id']}", headers=headers).json()["appid"] == "demo-app"
    app_document = client.get("/api/apps/demo-app", headers=headers).json()
    assert app_document["environment_keys"] == ["DATABASE_URL"]
    assert "mongodb://mongodb/demo" not in str(app_document)


def test_app_list_requires_authentication_and_returns_existing_apps() -> None:
    assert client.get("/api/apps").status_code == 401
    headers = auth_headers()
    first = f"list-first-{uuid4().hex}"
    second = f"list-second-{uuid4().hex}"
    for appid, name in ((first, "Zeta"), (second, "Alpha")):
        response = client.post(
            "/api/apps",
            headers=headers,
            json={
                "appid": appid,
                "name": name,
                "repository_url": "https://git.example/list.git",
                "compose": {"services": {"api": {"image": "example/list:latest"}}},
            },
        )
        assert response.status_code == 201

    response = client.get("/api/apps", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    selected = [item for item in payload if item["appid"] in {first, second}]
    assert [item["name"] for item in selected] == ["Alpha", "Zeta"]
    assert all("environment_ciphertext" not in item for item in selected)


def test_system_guide_requires_authentication_and_matches_routes() -> None:
    assert client.get("/api/system-guide").status_code == 401
    response = client.get("/api/system-guide", headers=auth_headers())
    assert response.status_code == 200
    guide = response.json()
    assert "POST /api/apps/{appid}/builds" in guide["call_sequence"]
    assert guide["polling"]["endpoint"] == "GET /api/builds/{build_id}"
    assert set(guide["polling"]["terminal_statuses"]) == {"succeeded", "failed", "cancelled"}
    assert guide["compose_rules"]["git_auto_discovery"] is False


def test_readme_returns_complete_markdown_guide() -> None:
    assert client.get("/api/readme").status_code == 401
    response = client.get("/api/readme", headers=auth_headers())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# API Reference" in response.text
    assert "# API 调用流程" in response.text
    assert "POST /api/apps/{appid}/builds" in response.text


def test_build_history_requires_authentication_and_supports_filters_pagination_and_detail() -> None:
    assert client.get("/api/builds").status_code == 401

    headers = auth_headers()
    appid = f"history-{uuid4().hex}"
    app_response = client.post(
        "/api/apps",
        headers=headers,
        json={
            "appid": appid,
            "name": "History",
            "repository_url": "https://git.example/history.git",
            "compose": {"services": {"api": {"image": "example/history:latest"}}},
        },
    )
    assert app_response.status_code == 201

    builds = []
    for git_ref in ("first", "second", "third"):
        response = client.post(
            f"/api/apps/{appid}/builds",
            headers=headers,
            json={"git_ref": git_ref},
        )
        assert response.status_code == 202
        builds.append(response.json())

    failed = app.state.container.builds.fail(builds[0]["build_id"], "history test failure")
    assert failed.status.value == "failed"

    page_one = client.get(
        "/api/builds",
        headers=headers,
        params={"appid": appid, "page": 1, "page_size": 2},
    )
    assert page_one.status_code == 200
    page_one_payload = page_one.json()
    assert page_one_payload["total"] == 3
    assert page_one_payload["page"] == 1
    assert page_one_payload["page_size"] == 2
    assert len(page_one_payload["items"]) == 2

    page_two = client.get(
        "/api/builds",
        headers=headers,
        params={"appid": appid, "page": 2, "page_size": 2},
    )
    assert page_two.status_code == 200
    page_two_payload = page_two.json()
    assert page_two_payload["total"] == 3
    assert len(page_two_payload["items"]) == 1
    page_one_ids = {item["build_id"] for item in page_one_payload["items"]}
    page_two_ids = {item["build_id"] for item in page_two_payload["items"]}
    assert page_one_ids.isdisjoint(page_two_ids)

    failed_page = client.get(
        "/api/builds",
        headers=headers,
        params={"appid": appid, "status": "failed"},
    )
    assert failed_page.status_code == 200
    assert failed_page.json()["total"] == 1
    assert failed_page.json()["items"][0]["build_id"] == builds[0]["build_id"]

    queued_page = client.get(
        "/api/builds",
        headers=headers,
        params={"appid": appid, "status": "queued"},
    )
    assert queued_page.status_code == 200
    assert queued_page.json()["total"] == 2
    assert {item["status"] for item in queued_page.json()["items"]} == {"queued"}

    detail = client.get(f"/api/builds/{builds[0]['build_id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["build_id"] == builds[0]["build_id"]
    assert detail.json()["appid"] == appid
    assert detail.json()["status"] == "failed"

    for invalid_params in ({"status": "unknown"}, {"page": 0}, {"page_size": 101}):
        assert client.get("/api/builds", headers=headers, params=invalid_params).status_code == 422


def test_app_access_returns_published_ports_and_configured_urls() -> None:
    settings = Settings(public_host="control.example.test", public_scheme="https")
    access_app = create_app(settings=settings)
    access_client = TestClient(access_app)
    headers = {
        "Authorization": "Bearer "
        + access_client.post(
            "/api/connect",
            json={"worker_name": "local-worker", "worker_secret": "local-worker-secret"},
        ).json()["access_token"]
    }
    appid = f"access-{uuid4().hex}"
    create_response = access_client.post(
        "/api/apps",
        headers=headers,
        json={
            "appid": appid,
            "name": "Access",
            "repository_url": "https://git.example/access.git",
            "compose": {"services": {"api": {"image": "example/access:latest"}}},
        },
    )
    assert create_response.status_code == 201
    access_app.state.container.repository.save_service(
        DeploymentService(
            service_id="service-access-api",
            appid=appid,
            build_id="build-access",
            service_name="access-api",
            image="example/access:latest",
            status="deployed",
            endpoint="access-api:8080",
            published_ports=[
                PublishedPort(target_port=80, published_port=8080),
                PublishedPort(target_port=8443, published_port=8443, protocol="udp"),
            ],
        )
    )

    response = access_client.get(f"/api/apps/{appid}/access", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["appid"] == appid
    assert payload["access_available"] is True
    assert payload["access_urls"] == ["https://control.example.test:8080"]
    assert payload["services"][0]["published_ports"][0]["published_port"] == 8080
    assert payload["services"][0]["access_urls"] == ["https://control.example.test:8080"]
    assert payload["services"][0]["access_available"] is True
    assert payload["services"][0]["access_reason"] is None
    legacy_services = access_client.get(f"/api/apps/{appid}/services", headers=headers)
    assert legacy_services.status_code == 200
    assert legacy_services.json()[0]["published_ports"][0]["published_port"] == 8080


def test_app_access_requires_authentication_and_reports_missing_app() -> None:
    assert client.get("/api/apps/no-such-app/access").status_code == 401
    headers = auth_headers()
    assert client.get("/api/apps/no-such-app/access", headers=headers).status_code == 404


def test_app_access_supports_legacy_endpoint_and_reports_unavailable_access() -> None:
    settings = Settings(public_host=None)
    access_app = create_app(settings=settings)
    access_client = TestClient(access_app)
    headers = auth_headers_for(access_client)
    appid = f"legacy-access-{uuid4().hex}"
    assert access_client.post(
        "/api/apps",
        headers=headers,
        json={
            "appid": appid,
            "name": "Legacy access",
            "repository_url": "https://git.example/legacy-access.git",
            "compose": {"services": {"api": {"image": "example/legacy:latest"}}},
        },
    ).status_code == 201
    access_app.state.container.repository.save_service(
        DeploymentService(
            service_id="service-legacy-api",
            appid=appid,
            build_id="build-legacy",
            service_name="legacy-api",
            image="example/legacy:latest",
            status="deployed",
            endpoint="legacy-api:9090",
        )
    )

    response = access_client.get(f"/api/apps/{appid}/access", headers=headers)
    assert response.status_code == 200
    service = response.json()["services"][0]
    assert service["published_ports"] == [
        {"target_port": 9090, "published_port": 9090, "protocol": "tcp", "mode": "ingress"}
    ]
    assert service["access_available"] is False
    assert service["access_urls"] == []
    assert service["access_reason"] == "Public host is not configured"
    assert response.json()["access_available"] is False


def auth_headers_for(test_client: TestClient) -> dict[str, str]:
    token = test_client.post(
        "/api/connect",
        json={"worker_name": "local-worker", "worker_secret": "local-worker-secret"},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_duplicate_app_and_missing_app_are_reported() -> None:
    headers = auth_headers()
    payload = {"appid": "duplicate-app", "name": "Duplicate", "repository_url": "https://git.example/demo.git"}
    assert client.post("/api/apps", headers=headers, json=payload).status_code == 201
    assert client.post("/api/apps", headers=headers, json=payload).status_code == 409
    assert client.post("/api/apps/missing/builds", headers=headers, json={}).status_code == 404


def test_build_requires_user_provided_compose_document() -> None:
    headers = auth_headers()
    payload = {"appid": "no-compose", "name": "No Compose", "repository_url": "https://git.example/no-compose.git"}
    assert client.post("/api/apps", headers=headers, json=payload).status_code == 201
    response = client.post("/api/apps/no-compose/builds", headers=headers, json={})
    assert response.status_code == 422
    assert "Compose document" in response.json()["detail"]


def test_template_composition_smoke() -> None:
    headers = auth_headers()
    listed = client.get("/api/templates", headers=headers)
    assert listed.status_code == 200
    assert {"python", "mongodb", "react"}.issubset(listed.json()["components"])

    response = client.post(
        "/api/templates/compose",
        headers=headers,
        json={"components": ["python", "mongodb", "react"], "dependencies": {"python": ["mongodb"]}},
    )
    assert response.status_code == 200
    document = response.json()
    assert document["version"] == "3.9"
    assert document["services"]["python"]["depends_on"] == ["mongodb"]
    assert document["services"]["mongodb"]["image"].startswith("mongo:")


def test_template_rejects_unknown_and_cyclic_dependencies() -> None:
    headers = auth_headers()
    unknown = client.post("/api/templates/compose", headers=headers, json={"components": ["python", "unknown"]})
    assert unknown.status_code == 422
    cyclic = client.post(
        "/api/templates/compose",
        headers=headers,
        json={"components": ["python", "mongodb"], "dependencies": {"python": ["mongodb"], "mongodb": ["python"]}},
    )
    assert cyclic.status_code == 422
