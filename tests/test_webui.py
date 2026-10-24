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
    assert "buildTaskRows" in page.text
    assert "/ui/app.js" in page.text
    assert stylesheet.status_code == 200
    assert "page-grid" in stylesheet.text
    assert ".toast-region" in stylesheet.text
    assert script.status_code == 200
    assert "/oauth2/token" in script.text
    assert "/api/v1/jenkins/build_list?" in script.text
    assert 'data-resource-refresh="access"' in page.text
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
    for section_id in (
        "appSection", "workspaceSection", "staticChecksSection", "buildSection", "dynamicChecksSection",
        "resourcesSection", "buildResourcesSection", "templatesSection", "baseImagesSection", "codexSettingsSection", "agentSettingsSection",
    ):
        assert f'href="#{section_id}"' in navigation_markup
        assert f'id="{section_id}"' in page.text
    assert navigation_markup.index('href="#appSection"') < navigation_markup.index('href="#workspaceSection"')
    assert navigation_markup.index('href="#workspaceSection"') < navigation_markup.index('href="#staticChecksSection"')
    assert navigation_markup.index('href="#staticChecksSection"') < navigation_markup.index('href="#buildSection"')
    assert navigation_markup.index('href="#buildSection"') < navigation_markup.index('href="#dynamicChecksSection"')
    assert "historySection" not in navigation_markup
    assert 'id="appListNavigation"' not in navigation_markup
    assert 'href="#operationSection"' not in navigation_markup
    assert 'id="applicationTab"' in navigation_markup
    assert 'id="globalTab"' in navigation_markup
    assert "应用详情" in navigation_markup
    assert "应用构建" in navigation_markup
    assert "静态检查" in navigation_markup
    assert "动态检查" in navigation_markup
    assert "资源回收" in navigation_markup
    assert "模板设置" in navigation_markup
    assert "镜像设置" in navigation_markup
    assert "Codex 配置" in navigation_markup
    assert "Agent 设置" in navigation_markup


def test_control_console_exposes_static_and_dynamic_route_check_controls() -> None:
    page = client.get("/ui/")
    script = client.get("/ui/app.js")
    assert page.status_code == 200
    for marker in (
        'id="requireRoute"',
        'name="require_route"',
        'id="staticChecksSection"',
        'id="routeCheckAppId"',
        'id="routeCheckCommitId"',
        'id="staticRouteCheckButton"',
        'id="staticCheckRefreshButton"',
        'id="routeCheckBuildId"',
        'id="dynamicRouteCheckButton"',
        'id="dynamicCheckRefreshButton"',
        'id="staticCheckTaskRows"',
        'id="dynamicCheckTaskRows"',
    ):
        assert marker in page.text
    assert 'id="staticCheckModal"' in page.text and 'id="staticCheckForm"' in page.text
    assert 'id="dynamicCheckModal"' in page.text and 'id="dynamicCheckForm"' in page.text
    assert 'id="taskDetailDrawer"' in page.text
    assert 'class="intro-band"' not in page.text
    assert 'id="selectedAppBanner"' not in page.text
    assert "/static-checks`" in script.text
    assert "/dynamic-checks`" in script.text
    assert "/check-results`" in script.text
    assert "/api/v1/jenkins/build_info/" in script.text
    assert "static_agent_role_version" in script.text
    stylesheet = client.get("/ui/styles.css")
    assert ".form-grid { align-items: start;" in stylesheet.text
    assert "height: 40px;" in stylesheet.text


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


def test_control_console_has_login_modal_and_no_implicit_app_context_modal() -> None:
    page = client.get("/ui/")
    assert page.status_code == 200

    modal = re.search(r'<section id="loginModal"[^>]*>', page.text)
    assert modal is not None
    assert 'class="modal-shell"' in modal.group(0)
    assert 'role="dialog"' in modal.group(0)
    assert 'aria-modal="true"' in modal.group(0)
    assert 'aria-labelledby="loginModalTitle"' in modal.group(0)
    assert 'id="loginForm"' in page.text
    for marker in (
        'id="openLoginModal"',
        'href="#loginModal"',
        'id="closeLoginModal"',
    ):
        assert marker in page.text
    assert 'id="appContextModal"' not in page.text
    assert 'id="contextAppSelect"' not in page.text
    assert 'id="openConnectionModal"' not in page.text
    assert "连接设置" not in page.text


def test_control_console_exposes_topbar_session_and_app_status_markers() -> None:
    page = client.get("/ui/")
    stylesheet = client.get("/ui/styles.css")
    script = client.get("/ui/app.js")
    assert page.status_code == 200
    assert stylesheet.status_code == 200
    assert script.status_code == 200

    for marker in (
        'id="appSelector"',
        'id="sessionMenu"',
        'id="sessionMenuToggle"',
        'id="sessionStatus"',
        'id="healthButton"',
        'id="openLoginModal"',
        'id="disconnectButton"',
    ):
        assert marker in page.text
    assert 'class="status-dot offline"' in page.text
    assert ".status-dot.offline::before" in stylesheet.text
    assert "function setSession" in script.text
    assert '$("sessionStatus")' in script.text
    assert 'id="selectedAppTitle"' not in page.text
    assert "CONTROL PLANE" not in page.text


def test_control_console_declares_east_eight_display_timezone() -> None:
    page = client.get("/ui/")
    script = client.get("/ui/app.js")
    assert 'const DISPLAY_TIME_ZONE = "Asia/Shanghai"' in script.text
    assert 'timeZone: DISPLAY_TIME_ZONE' in script.text
    assert "toLocaleTimeString()" not in script.text
    assert "toLocaleString()" not in script.text


def test_control_console_loads_apps_from_the_application_list() -> None:
    page = client.get("/ui/")
    script = client.get("/ui/app.js")
    assert page.status_code == 200
    assert 'id="appListSection"' in page.text
    assert 'id="appList"' in page.text
    assert 'id="refreshAppsButton"' in page.text
    assert 'id="newAppButton"' in page.text
    assert "/api/v1/jenkins/app_list" in script.text
    assert "/api/v1/docker/compose_prompt" in script.text
    assert "function loadApps" in script.text
    assert "function openApplication" in script.text
    assert 'const UI_STATE_KEY = "orchestrator.control.ui-state"' in script.text
    assert "function persistUiState" in script.text
    assert 'localStorage.setItem(UI_STATE_KEY, JSON.stringify(value))' in script.text


def test_app_workspaces_are_nested_under_the_application_list() -> None:
    page = client.get("/ui/")
    stylesheet = client.get("/ui/styles.css")
    script = client.get("/ui/app.js")
    for section in ("appSection", "workspaceSection", "staticChecksSection", "buildSection", "dynamicChecksSection", "buildResourcesSection", "resourcesSection"):
        assert f'href="#{section}" data-navigation-scope="application"' in page.text
    assert 'id="historyAppId" placeholder="从顶部选择应用" autocomplete="off" readonly' in page.text
    assert 'id="appSelector"' in page.text
    assert 'id="routeCheckCommitId"' in page.text and '<select id="routeCheckCommitId"' in page.text
    assert 'id="buildId" type="hidden"' in page.text
    assert 'id="buildResourcesId" required disabled' in page.text and '<select id="buildResourcesId"' in page.text
    assert 'id="allHistoryNavigation"' not in page.text
    assert '全部任务' not in page.text
    assert '全部应用' not in page.text
    assert 'id="agentRoleForm"' in page.text
    assert 'id="agentRoleSelect"' in page.text
    assert 'id="agentRoleSchema"' in page.text
    assert "state.historyScope" not in script.text
    assert "历史任务响应包含其他应用的数据，已拒绝显示" in script.text
    assert "路由检查仅允许操作顶部当前选择的应用" in script.text
    assert "GET /api/v1/apps/{app_id}/identifiers" in script.text or "/identifiers" in script.text
    assert "html, body { height: 100%; overflow: hidden; }" in stylesheet.text
    assert ".workspace {" in stylesheet.text and "overflow-y: auto" in stylesheet.text
    assert "打开应用后加载该应用的历史任务" in script.text


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
        "serviceAccessRegion",
    ):
        assert f'id="{element_id}"' in page.text
    assert "/api/v1/jenkins/app_sync/" in script.text


def test_build_workspace_distinguishes_receipt_and_automatic_status_queries() -> None:
    page = client.get("/ui/")
    script = client.get("/ui/app.js")
    assert 'id="buildPollStatus"' in page.text
    assert "HTTP 202 表示任务已受理" in page.text
    assert "约 5 秒后再次查询" in script.text
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
    assert "if (!state.appIdentifiers.builds.some((build) => build.build_id === buildId))" in script
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
