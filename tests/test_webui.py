import re

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
    assert 'data-resource="access"' in page.text
    assert 'resource === "access"' in script.text
    assert "serviceAccessTable" in page.text
    assert "未发布端口" in script.text
    assert "notifyError" in script.text


def test_control_console_sidebar_navigation_targets_each_workspace_section() -> None:
    page = client.get("/ui/")
    assert page.status_code == 200

    navigation = re.search(r'<nav class="side-nav"[^>]*>(.*?)</nav>', page.text, flags=re.DOTALL)
    assert navigation is not None
    navigation_markup = navigation.group(1)
    section_ids = (
        "appSection",
        "buildSection",
        "historySection",
        "resourcesSection",
        "templatesSection",
        "baseImagesSection",
    )
    for section_id in section_ids:
        assert f'href="#{section_id}"' in navigation_markup
        assert f'id="{section_id}"' in page.text


def test_control_console_has_connection_and_app_context_modals() -> None:
    page = client.get("/ui/")
    assert page.status_code == 200

    for modal_id, title_id in (
        ("connectionModal", "connectionModalTitle"),
        ("appContextModal", "appContextModalTitle"),
    ):
        modal = re.search(fr'<section id="{modal_id}"[^>]*>', page.text)
        assert modal is not None
        modal_markup = modal.group(0)
        assert 'class="modal-shell"' in modal_markup
        assert 'role="dialog"' in modal_markup
        assert 'aria-modal="true"' in modal_markup
        assert f'aria-labelledby="{title_id}"' in modal_markup
        assert f'id="{title_id}"' in page.text
    for marker in (
        'id="openConnectionModal"',
        'href="#connectionModal"',
        'id="closeConnectionModal"',
        'id="openAppContextModal"',
        'href="#appContextModal"',
        'id="closeAppContextModal"',
    ):
        assert marker in page.text
    assert 'id="connectForm"' in page.text
    assert 'id="contextAppSelect"' in page.text
    assert 'id="refreshContextAppsButton"' in page.text


def test_control_console_exposes_topbar_session_and_app_status_markers() -> None:
    page = client.get("/ui/")
    stylesheet = client.get("/ui/styles.css")
    script = client.get("/ui/app.js")
    assert page.status_code == 200
    assert stylesheet.status_code == 200
    assert script.status_code == 200

    for marker in (
        'class="topbar-context"',
        'id="currentAppStatus"',
        'id="metricApp"',
        'id="sessionStatus"',
    ):
        assert marker in page.text
    assert 'class="status-dot offline"' in page.text
    assert 'id="metricApp">未设置</strong>' in page.text
    assert ".status-dot.offline::before" in stylesheet.text
    assert "function setSession" in script.text
    assert '$("sessionStatus")' in script.text
    assert '$("metricApp")' in script.text


def test_control_console_loads_existing_apps_for_context_selection() -> None:
    page = client.get("/ui/")
    script = client.get("/ui/app.js")
    assert page.status_code == 200
    assert 'id="contextAppSelect"' in page.text
    assert 'id="loadContextButton"' in page.text
    assert "/api/apps" in script.text
    assert "loadAppChoices" in script.text
