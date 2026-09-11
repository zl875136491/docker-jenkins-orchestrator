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
