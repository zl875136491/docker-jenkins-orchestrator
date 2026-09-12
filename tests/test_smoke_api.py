from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def test_healthz_smoke() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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


def test_duplicate_app_and_missing_app_are_reported() -> None:
    headers = auth_headers()
    payload = {"appid": "duplicate-app", "name": "Duplicate", "repository_url": "https://git.example/demo.git"}
    assert client.post("/api/apps", headers=headers, json=payload).status_code == 201
    assert client.post("/api/apps", headers=headers, json=payload).status_code == 409
    assert client.post("/api/apps/missing/builds", headers=headers, json={}).status_code == 404


def test_build_requires_compose_document() -> None:
    headers = auth_headers()
    payload = {"appid": "no-compose", "name": "No Compose", "repository_url": "https://git.example/no-compose.git"}
    assert client.post("/api/apps", headers=headers, json=payload).status_code == 201
    response = client.post("/api/apps/no-compose/builds", headers=headers, json={})
    assert response.status_code == 422


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
