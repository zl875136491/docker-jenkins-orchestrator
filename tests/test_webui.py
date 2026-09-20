from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def test_control_console_redirects_to_static_ui() -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"


def test_control_console_assets_are_served_without_api_authentication() -> None:
    page = client.get("/ui/")
    stylesheet = client.get("/ui/styles.css")
    script = client.get("/ui/app.js")

    assert page.status_code == 200
    assert "Orchestrator" in page.text
    assert 'id="toastRegion"' in page.text
    assert "历史任务" in page.text
    assert "historySection" in page.text
    assert "/ui/app.js" in page.text
    assert stylesheet.status_code == 200
    assert "page-grid" in stylesheet.text
    assert ".toast-region" in stylesheet.text
    assert script.status_code == 200
    assert "/api/connect" in script.text
    assert "/api/builds?" in script.text
    assert "notifyError" in script.text
