from uuid import uuid4

from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def auth_headers() -> dict[str, str]:
    token = client.post(
        "/oauth2/token",
        json={"client_id": "local-worker", "client_secret": "local-worker-secret"},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def stack_payload(stack_id: str) -> dict:
    json_data = {
        "images": ["example/web:1.2.3"],
        "port": 8088,
        "publish_ports": True,
        "restart": "unless-stopped",
        "environment": {"PORT": "8088", "APP_MODE": "change-me"},
    }
    return {
        "tech_stack_id": stack_id,
        "name": "Example Web",
        "yaml_original": (
            "images:\n"
            "  - example/web:1.2.3\n"
            "port: 8088\n"
            "publish_ports: true\n"
            "restart: unless-stopped\n"
            "environment:\n"
            "  PORT: '8088'\n"
            "  APP_MODE: change-me\n"
        ),
        "json_data": json_data,
        "line_comments": {
            "$.environment.APP_MODE": "用户必须根据项目实际启动方式补充。",
            "$.port": "确认应用在容器内监听此端口。",
        },
    }


def test_tech_stack_crud_keeps_yaml_json_and_aligned_comments() -> None:
    headers = auth_headers()
    defaults = client.get("/api/v1/docker/tech_stack_list", headers=headers)
    assert defaults.status_code == 200
    assert {item["tech_stack_id"] for item in defaults.json()} >= {"python", "react", "mongodb"}
    assert all({"yaml_original", "json_data", "line_comments"} <= set(item) for item in defaults.json())
    stack_id = f"custom-{uuid4().hex}"
    created = client.post("/api/v1/docker/tech_stack_create", headers=headers, json=stack_payload(stack_id))
    assert created.status_code == 201, created.text
    value = created.json()
    assert value["tech_stack_id"] == stack_id
    assert value["line_comments"]["$.environment.APP_MODE"]
    assert "$.images[0]" in value["line_comments"]
    assert value["line_comments"]["$.images[0]"] == ""

    listed = client.get("/api/v1/docker/tech_stack_list", headers=headers)
    assert listed.status_code == 200
    assert any(item["tech_stack_id"] == stack_id for item in listed.json())

    updated = client.patch(
        f"/api/v1/docker/tech_stack_info/{stack_id}",
        headers=headers,
        json={"name": "Updated Web", "line_comments": {"$.port": "新的端口说明"}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Updated Web"
    assert updated.json()["line_comments"]["$.port"] == "新的端口说明"
    assert updated.json()["line_comments"]["$.images[0]"] == ""

    composed = client.post(
        "/api/v1/docker/template_compose",
        headers=headers,
        json={"components": [stack_id]},
    )
    assert composed.status_code == 200, composed.text
    assert composed.json()["services"][stack_id]["image"] == "example/web:1.2.3"

    deleted = client.delete(f"/api/v1/docker/tech_stack_info/{stack_id}", headers=headers)
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/docker/tech_stack_info/{stack_id}", headers=headers).status_code == 404


def test_tech_stack_requires_yaml_json_equality_and_rejects_unknown_comment_paths() -> None:
    headers = auth_headers()
    payload = stack_payload(f"invalid-{uuid4().hex}")
    payload["line_comments"] = {"$.does_not_exist": "wrong"}
    response = client.post("/api/v1/docker/tech_stack_create", headers=headers, json=payload)
    assert response.status_code == 422
    assert "line_comments" in response.text

    payload = stack_payload(f"mismatch-{uuid4().hex}")
    payload["yaml_original"] = "images: [example/web:9.9.9]\nport: 8088\n"
    response = client.post("/api/v1/docker/tech_stack_create", headers=headers, json=payload)
    assert response.status_code == 422
    assert "json_data" in response.text


def test_compose_prompt_is_authenticated_markdown() -> None:
    assert client.get("/api/v1/docker/compose_prompt").status_code == 401
    response = client.get("/api/v1/docker/compose_prompt", headers=auth_headers())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "docker-compose.yaml" in response.text
    assert "/api/v1/docker/tech_stack_create" in response.text
    assert "## 当前可用技术栈模板" in response.text
