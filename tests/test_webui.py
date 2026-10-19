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
    assert "/oauth2/token" in script.text
    assert "/api/v1/jenkins/build_list?" in script.text
    assert 'data-resource="access"' in page.text
    assert 'id="loadComposePromptButton"' in page.text
    assert 'id="composePromptOutput"' in page.text
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
        "workspaceSection",
        "buildSection",
        "historySection",
        "resourcesSection",
        "templatesSection",
        "baseImagesSection",
    )
    for section_id in section_ids:
        assert f'href="#{section_id}"' in navigation_markup
        assert f'id="{section_id}"' in page.text


def test_control_console_exposes_workspace_upload_listing_deletion_and_pagination() -> None:
    page = client.get("/ui/")
    script = client.get("/ui/app.js")
    for element_id in (
        "workspaceAppId", "workspaceUploadForm", "workspaceFiles", "workspaceDirectory",
        "workspaceUploadPath", "workspaceUploadButton", "workspaceRefreshButton", "workspaceStatus",
        "workspaceFileRows", "workspaceEmpty", "workspaceSearch", "workspacePageSize",
        "workspacePreviousButton", "workspaceNextButton", "workspacePageLabel",
    ):
        assert f'id="{element_id}"' in page.text
    assert "webkitdirectory" in page.text
    assert "已有构建快照" in page.text
    assert "new FormData()" in script.text
    assert 'form.append("files", entry.file, path)' in script.text
    assert "/api/v1/workspace/" in script.text
    assert "deleteWorkspaceFile" in script.text
    assert "workspaceContextMatches" in script.text


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


def test_control_console_declares_east_eight_display_timezone() -> None:
    page = client.get("/ui/")
    script = client.get("/ui/app.js")
    assert "时间统一使用东八区（UTC+08:00）" in page.text
    assert 'const DISPLAY_TIME_ZONE = "Asia/Shanghai"' in script.text
    assert 'timeZone: DISPLAY_TIME_ZONE' in script.text
    assert "toLocaleTimeString()" not in script.text
    assert "toLocaleString()" not in script.text


def test_control_console_loads_existing_apps_for_context_selection() -> None:
    page = client.get("/ui/")
    script = client.get("/ui/app.js")
    assert page.status_code == 200
    assert 'id="contextAppSelect"' in page.text
    assert 'id="loadContextButton"' in page.text
    assert "/api/v1/jenkins/app_list" in script.text
    assert "/api/v1/docker/compose_prompt" in script.text
    assert "loadAppChoices" in script.text


def test_control_console_exposes_compose_discovery_and_repository_sync_results() -> None:
    page = client.get("/ui/")
    script = client.get("/ui/app.js")

    assert page.status_code == 200
    assert script.status_code == 200
    for element_id in (
        "composeHelp",
        "composeSourcePanel",
        "composeSourceStatus",
        "composeSourceFile",
        "composeSourceCommit",
        "syncAppButton",
        "syncStatus",
        "syncResultPanel",
        "syncCommitId",
        "syncPreviousCommitId",
        "syncComposeChanged",
        "syncCommitChanged",
        "syncGitRef",
        "syncComposeFile",
        "syncUpdatedAt",
        "appSyncOutput",
        "buildCommit",
        "serviceAccessRegion",
    ):
        assert f'id="{element_id}"' in page.text
    assert "/api/v1/jenkins/app_sync/" in script.text


def test_build_workspace_distinguishes_receipt_and_automatic_status_queries() -> None:
    page = client.get("/ui/")
    script = client.get("/ui/app.js")
    assert 'id="buildPollStatus"' in page.text
    assert "HTTP 202 仅表示任务已受理" in page.text
    assert "约每 5 秒自动查询" in page.text
    assert "const BUILD_POLL_INTERVAL_MS = 5000" in script.text
    assert 'window.addEventListener("pagehide"' in script.text
    assert "状态未刷新" in script.text


def test_build_requests_share_identity_validation_and_keep_submission_separate_from_polling() -> None:
    script = client.get("/ui/app.js").text
    create = re.search(r"async function createBuild\(\) \{(.*?)\n  async function getBuild", script, flags=re.DOTALL)
    assert create is not None
    assert "if (state.buildCreateOperation) return null;" in create.group(1)
    assert create.group(1).index("state.buildCreateOperation = operation") < create.group(1).index("await request")
    assert "state.buildPollController = controller" not in create.group(1)
    assert "state.buildCreateOperation === operation" in create.group(1)
    assert 'form.setAttribute("aria-busy", "true")' in script
    assert 'Object.prototype.hasOwnProperty.call(build, "appid")' in script
    assert "appid !== undefined && build.appid !== appid" in script
    assert "state.latestBuild?.build_id === buildId ? state.buildAppid : undefined" in script
    assert "validateBuildResponse(value, buildId, expectedAppid)" in script
    assert "validateBuildResponse(value, buildId, selectedAppid)" in script


def test_history_inspection_does_not_wait_for_or_reapply_build_status_from_resources() -> None:
    script = client.get("/ui/app.js").text
    inspect = re.search(r"async function inspectBuild\(buildId, appid\) \{(.*?)\n  function stopHistoryPolling", script, flags=re.DOTALL)
    assert inspect is not None
    source = inspect.group(1)
    assert source.index("loadHistoryDetailResources(operation);") < source.index("await request")
    assert "Promise.allSettled" not in source
    assert "startBuildPolling(value, { inspect: true, appid: selectedAppid })" in source
    resources = re.search(r"async function loadHistoryDetailResources\(operation\) \{(.*?)\n  async function inspectBuild", script, flags=re.DOTALL)
    assert resources is not None
    assert "historyDetailContextMatches(operation)" in resources.group(1)
    assert "applyBuild(" not in resources.group(1)


def test_template_workspace_exposes_technology_stack_editor_and_compose_actions() -> None:
    page = client.get("/ui/")
    script = client.get("/ui/app.js")
    stylesheet = client.get("/ui/styles.css")
    assert page.status_code == 200
    assert script.status_code == 200
    assert stylesheet.status_code == 200
    for marker in (
        'id="techStackSearch"',
        'id="techStackList"',
        'id="techStackForm"',
        'id="techStackId"',
        'id="techStackName"',
        'id="techStackYaml"',
        'id="techStackJson"',
        'id="techStackComments"',
        'id="newTechStackButton"',
        'id="createTechStackButton"',
        'id="saveTechStackButton"',
        'id="deleteTechStackButton"',
        'id="resetTechStackButton"',
        'id="copyComposePromptButton"',
        'id="templateFormat"',
    ):
        assert marker in page.text
    for path in (
        "/api/v1/docker/tech_stack_list",
        "/api/v1/docker/tech_stack_create",
        "/api/v1/docker/tech_stack_info/",
        "/api/v1/docker/template_compose",
        "/api/v1/docker/compose_prompt",
    ):
        assert path in script.text
    for marker in ("renderTechStackList", "saveTechStack", "deleteTechStack", "copyText"):
        assert marker in script.text
    assert ".tech-stack-workspace" in stylesheet.text
