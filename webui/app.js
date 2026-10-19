(() => {
  "use strict";

  const TOKEN_KEY = "orchestrator.control.token";
  const REFRESH_TOKEN_KEY = "orchestrator.control.refresh-token";
  const APP_KEY = "orchestrator.control.appid";
  const APP_NAME_KEY = "orchestrator.control.appname";
  const DISPLAY_TIME_ZONE = "Asia/Shanghai";
  const TOAST_TIMEOUT_MS = 5000;
  const BUILD_POLL_INTERVAL_MS = 5000;
  const BUILD_POLL_MAX_RETRY_MS = 30000;
  const notifiedErrors = new WeakSet();
  const state = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    refreshToken: localStorage.getItem(REFRESH_TOKEN_KEY) || "",
    appid: localStorage.getItem(APP_KEY) || "",
    appName: localStorage.getItem(APP_NAME_KEY) || "",
    activeSection: "appSection",
    buildId: "",
    buildAppid: "",
    latestBuild: null,
    buildPollTimer: null,
    buildPollController: null,
    buildPollGeneration: 0,
    buildPollFailures: 0,
    buildLastRefreshedAt: "",
    buildCreateOperation: null,
    historyDetailOperation: null,
    historyPage: 1,
    historyPageSize: 20,
    historyTotal: 0,
    historyLoading: false,
    historyTimer: null,
    techStacks: [],
    selectedTechStackId: "",
    loadedApp: null,
    environmentDirty: false,
    environmentBaseline: "{}",
    appContextVersion: 0,
    appOperation: null,
    syncRefreshPendingAppid: "",
    workspaceAppid: "",
    workspaceFiles: [],
    workspaceTotalBytes: 0,
    workspaceLoaded: false,
    workspacePage: 1,
    workspacePageSize: 20,
    workspaceOperation: null,
    resourceBuildReport: null,
    resourceBuildController: null,
    resourceBuildTimer: null,
    resourceBuildGeneration: 0,
    resourceBuildBusy: false,
  };

  const $ = (id) => document.getElementById(id);
  const apiOrigin = window.location.origin;
  $("apiOrigin").textContent = apiOrigin;

  class ApiError extends Error {
    constructor(status, payload) {
      const detail = payload && payload.detail;
      const detailMessage = Array.isArray(detail)
        ? detail.map((item) => item.msg || JSON.stringify(item)).join("; ")
        : typeof detail === "string"
          ? detail
          : detail
            ? JSON.stringify(detail)
            : "";
      const message = detailMessage || payload?.message || `请求失败（HTTP ${status}）`;
      super(message);
      this.status = status;
      this.payload = payload;
    }
  }

  function errorMessage(error) {
    if (error instanceof TypeError && error.message === "Failed to fetch") {
      return "无法连接控制端，请检查网络或服务状态";
    }
    return error instanceof Error && error.message ? error.message : String(error || "请求失败");
  }

  function dismissToast(toast) {
    if (!toast || toast.dataset.dismissed === "true") return;
    toast.dataset.dismissed = "true";
    window.clearTimeout(toast.dismissTimer);
    toast.classList.remove("toast-visible");
    window.setTimeout(() => toast.remove(), 180);
  }

  function showToast(message, tone = "error") {
    const region = $("toastRegion");
    if (!region) return;
    const toast = document.createElement("div");
    toast.className = `toast toast-${tone}`;
    toast.setAttribute("role", tone === "error" ? "alert" : "status");

    const content = document.createElement("div");
    content.className = "toast-content";
    const title = document.createElement("strong");
    title.className = "toast-title";
    title.textContent = tone === "error" ? "请求失败" : "提示";
    const detail = document.createElement("span");
    detail.className = "toast-message";
    detail.textContent = String(message || "请求失败");
    content.append(title, detail);

    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "关闭提示");
    close.textContent = "×";
    close.addEventListener("click", () => dismissToast(toast));
    toast.append(content, close);
    region.appendChild(toast);
    toast.dismissTimer = window.setTimeout(() => dismissToast(toast), TOAST_TIMEOUT_MS);
    window.requestAnimationFrame(() => toast.classList.add("toast-visible"));
  }

  function notifyError(error) {
    const message = errorMessage(error);
    if (error && typeof error === "object") {
      if (notifiedErrors.has(error)) return message;
      notifiedErrors.add(error);
    }
    showToast(message);
    return message;
  }

  function setActivity(message, tone = "") {
    const target = $("activityMessage");
    target.textContent = message;
    target.className = tone ? `activity-${tone}` : "";
  }

  function setSession(online, detail = "") {
    const status = $("sessionStatus");
    status.textContent = online ? "已连接" : "未连接";
    status.className = `status-dot ${online ? "online" : "offline"}`;
    $("sessionDetail").textContent = detail || (online ? "JWT 已保存到当前浏览器。" : "请输入 client 凭据获取 JWT。");
    renderWorkspace();
  }

  function setCurrentApp(name = "") {
    state.appName = String(name || "").trim();
    if (state.appName) {
      localStorage.setItem(APP_NAME_KEY, state.appName);
    } else {
      localStorage.removeItem(APP_NAME_KEY);
    }
    const target = $("metricApp");
    if (target) target.textContent = state.appName || "未设置";
  }

  function sectionFromHash() {
    const candidate = window.location.hash.slice(1);
    return $(candidate)?.classList.contains("workspace-section") ? candidate : "";
  }

  function rememberSection() {
    const section = sectionFromHash();
    if (section) state.activeSection = section;
  }

  function closeModal(modalId) {
    const modal = $(modalId);
    if (!modal) return;
    if (window.location.hash === `#${modalId}`) {
      window.location.hash = state.activeSection || "appSection";
    }
    modal.removeAttribute("data-open");
  }

  function bindModals() {
    document.querySelectorAll(".modal-shell").forEach((modal) => {
      const modalId = modal.id;
      modal.querySelectorAll(".modal-backdrop, .modal-close").forEach((control) => {
        control.addEventListener("click", (event) => {
          event.preventDefault();
          closeModal(modalId);
        });
      });
    });
    document.querySelectorAll(".modal-trigger").forEach((trigger) => {
      trigger.addEventListener("click", () => rememberSection());
    });
    window.addEventListener("hashchange", () => {
      const modal = document.querySelector(".modal-shell:target");
      if (modal) modal.setAttribute("data-open", "true");
      rememberSection();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const modal = document.querySelector(".modal-shell:target");
      if (modal) closeModal(modal.id);
    });
  }

  function syncNavigation() {
    const section = sectionFromHash();
    if (section) state.activeSection = section;
    const activeSection = section || state.activeSection || "appSection";
    document.querySelectorAll(".workspace-section").forEach((item) => {
      const active = item.id === activeSection;
      item.classList.toggle("active", active);
      item.toggleAttribute("aria-hidden", !active);
    });
    document.querySelectorAll(".side-nav a[href^='#']").forEach((link) => {
      const active = link.getAttribute("href") === `#${activeSection}`;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    activateWorkspace();
  }

  function bindNavigation() {
    document.querySelectorAll(".side-nav a[href^='#']").forEach((link) => {
      link.addEventListener("click", () => {
        const sectionId = link.getAttribute("href").slice(1);
        if ($(sectionId)?.classList.contains("workspace-section")) {
          state.activeSection = sectionId;
          syncNavigation();
        }
      });
    });
    window.addEventListener("hashchange", syncNavigation);
    syncNavigation();
  }

  function pretty(targetId, value) {
    $(targetId).textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }

  function prettyNode(target, value) {
    target.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }

  function statusClass(status) {
    const normalized = String(status || "").toLowerCase();
    if (normalized === "succeeded") return "success";
    if (normalized === "failed" || normalized === "cancelled") return "failed";
    if (normalized) return "active";
    return "neutral";
  }

  function formatDate(value) {
    if (!value) return "--";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { timeZone: DISPLAY_TIME_ZONE, hourCycle: "h23" });
  }

  function formatTime(value = new Date()) {
    return value.toLocaleTimeString("zh-CN", { timeZone: DISPLAY_TIME_ZONE, hourCycle: "h23" });
  }

  function parsedJson(text, label, allowEmpty = false) {
    if (!text.trim() && allowEmpty) return undefined;
    try {
      return JSON.parse(text);
    } catch (error) {
      throw new Error(`${label} 不是有效 JSON`);
    }
  }

  function composeJson() {
    const value = parsedJson($("compose").value, "Compose", true);
    if (value !== undefined && (!value || typeof value !== "object" || Array.isArray(value))) {
      throw new Error("Compose 必须是 JSON 对象；留空会在创建时自动发现，在更新时保留原值");
    }
    return value;
  }

  function setSyncStatus(message, tone = "") {
    const target = $("syncStatus");
    target.textContent = message;
    target.className = `operation-status small ${tone ? `activity-${tone}` : "muted"}`;
  }

  function resetAppView() {
    resetWorkspaceView();
    stopBuildCreation();
    stopBuildPolling("应用已切换，自动查询已停止。", "muted");
    resetBuildView();
    state.loadedApp = null;
    state.syncRefreshPendingAppid = "";
    state.environmentDirty = false;
    state.environmentBaseline = "{}";
    state.appContextVersion += 1;
    $("environment").value = "{}";
    $("environment").placeholder = "{}";
    $("environmentHelp").textContent = "创建时使用此 JSON；读取应用只返回变量名，不返回密钥值。更新时留空或不修改会保留原有环境变量；明确填写 {} 会清空它们。";
    $("composeSourceStatus").textContent = "尚未读取当前应用，来源未确认。编辑器中的内容可能是未保存草稿。";
    ["composeSourceFile", "composeSourceCommit", "composeSourceRef"].forEach((id) => { $(id).textContent = "--"; });
    $("composeSourcePanel").removeAttribute("data-appid");
    $("serviceAccessRegion").textContent = "--";
    $("syncResultPanel").hidden = true;
    pretty("appSyncOutput", "等待同步操作");
    setSyncStatus("同步使用已保存的仓库和 Git 引用，覆盖已保存的 Compose；不会自动构建。");
  }

  function renderComposeSource(app) {
    const hasCompose = app.compose !== null && app.compose !== undefined;
    const fromRepository = Boolean(app.compose_source_file || app.compose_source_commit);
    $("composeSourcePanel").dataset.appid = app.appid || "";
    $("composeSourceStatus").textContent = !hasCompose
      ? "当前应用没有已保存的 Compose；从仓库同步可发现并保存配置。"
      : fromRepository
        ? "仓库发现的 Compose（已保存）。编辑器修改在点击更新前不会改变此来源记录。"
        : "手动配置的 Compose（无仓库来源元数据）；可同步为仓库中的配置。";
    $("composeSourceRef").textContent = app.git_ref || "--";
    $("composeSourceFile").textContent = app.compose_source_file || "--";
    $("composeSourceCommit").textContent = app.compose_source_commit || "--";
  }

  function sameAppContext(appid, version) {
    return state.appid === appid && $("appId").value.trim() === appid && state.appContextVersion === version;
  }

  function ensureAppIdle() {
    if (state.appOperation) throw new Error("应用操作进行中，请等待完成后再操作");
  }

  function beginAppOperation(label, appid) {
    ensureAppIdle();
    state.appContextVersion += 1;
    const operation = {
      appid,
      version: state.appContextVersion,
      activeAppid: state.appid,
      inputAppid: $("appId").value.trim(),
      controls: [],
      links: [],
      timer: null,
    };
    state.appOperation = operation;
    document.querySelectorAll("#appForm input, #appForm textarea, #appForm button, #appForm select, #appContextModal button, #appContextModal select, #buildForm input, #buildForm button, #connectForm input, #connectForm button, #templateForm button").forEach((control) => {
      operation.controls.push([control, control.disabled]);
      control.disabled = true;
    });
    ["disconnectButton", "syncAppButton"].forEach((id) => {
      const control = $(id);
      if (control && !operation.controls.some(([existing]) => existing === control)) {
        operation.controls.push([control, control.disabled]);
        control.disabled = true;
      }
    });
    ["openAppContextModal", "openConnectionModal", "workspaceSelectAppButton"].forEach((id) => {
      const link = $(id);
      if (!link) return;
      operation.links.push([link, link.getAttribute("aria-disabled"), link.getAttribute("tabindex")]);
      link.setAttribute("aria-disabled", "true");
      link.setAttribute("tabindex", "-1");
    });
    $("appForm").setAttribute("aria-busy", "true");
    $("composeSourcePanel").setAttribute("aria-busy", "true");
    $("syncAppButton").textContent = label;
    renderWorkspace();
    return operation;
  }

  function finishAppOperation(operation) {
    if (state.appOperation !== operation) return;
    window.clearInterval(operation.timer);
    operation.controls.forEach(([control, disabled]) => { control.disabled = disabled; });
    operation.links.forEach(([link, ariaDisabled, tabIndex]) => {
      if (ariaDisabled === null) link.removeAttribute("aria-disabled");
      else link.setAttribute("aria-disabled", ariaDisabled);
      if (tabIndex === null) link.removeAttribute("tabindex");
      else link.setAttribute("tabindex", tabIndex);
    });
    $("appForm").removeAttribute("aria-busy");
    $("composeSourcePanel").removeAttribute("aria-busy");
    $("syncAppButton").textContent = "同步仓库 Compose";
    state.appOperation = null;
    updateBuildSubmitState();
    renderWorkspace();
    activateWorkspace();
  }

  async function withAppOperation(label, appid, action) {
    const operation = beginAppOperation(label, appid);
    try {
      return await action(operation);
    } finally {
      finishAppOperation(operation);
    }
  }

  function appidFromInput() {
    const value = $("appId").value.trim() || state.appid;
    if (!value) throw new Error("请先填写 App ID");
    if (value !== state.appid) {
      setCurrentApp("");
      resetAppView();
    }
    state.appid = value;
    localStorage.setItem(APP_KEY, value);
    $("appId").value = value;
    const selector = $("contextAppSelect");
    if (selector) selector.value = value;
    renderWorkspace();
    return value;
  }

  function applyApp(app) {
    if (state.appid !== app.appid) resetAppView();
    state.appid = app.appid || "";
    localStorage.setItem(APP_KEY, state.appid);
    setCurrentApp(app.name || "");
    $("appId").value = app.appid || "";
    const selector = $("contextAppSelect");
    if (selector) selector.value = app.appid || "";
    $("appName").value = app.name || "";
    $("repositoryUrl").value = app.repository_url || "";
    $("gitRef").value = app.git_ref || "main";
    $("components").value = (app.components || []).join(",");
    $("compose").value = app.compose !== null && app.compose !== undefined ? JSON.stringify(app.compose, null, 2) : "";
    state.loadedApp = JSON.parse(JSON.stringify(app));
    state.environmentDirty = false;
    state.environmentBaseline = "";
    $("environment").value = "";
    $("environment").placeholder = "留空保留已有环境变量；填写完整 JSON 可替换";
    const keys = (app.environment_keys || []).join(", ") || "无";
    $("environmentHelp").textContent = `已保存的变量名：${keys}。API 不返回密钥值；未修改或留空不会替换原值。填写完整 JSON 会替换全部环境变量，明确填写 {} 会清空它们。`;
    renderComposeSource(app);
    if (state.syncRefreshPendingAppid === app.appid) {
      state.syncRefreshPendingAppid = "";
      setSyncStatus("仓库同步成功，并已重新读取应用配置和来源。未触发构建，确认后请显式提交构建。", "success");
    }
    renderWorkspace();
  }

  function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  }

  function workspaceEntries() {
    const files = Array.from($("workspaceFiles").files);
    const directory = Array.from($("workspaceDirectory").files);
    return (files.length ? files : directory).map((file) => ({
      file,
      path: file.webkitRelativePath ? file.webkitRelativePath.split("/").slice(1).join("/") : file.name,
    }));
  }

  function workspaceRelativePath(value, allowRoot = false) {
    const parts = value.split("/").filter((part) => part && part !== ".");
    if (value.length > 1024 || /^[\/~]/.test(value) || /[\\:\u0000-\u001f]/.test(value)
      || parts.some((part) => part === ".." || part === ".git") || (!allowRoot && !parts.length)) {
      throw new Error("文件路径必须是相对 POSIX 路径，禁止 ..、.git、绝对路径、反斜杠和控制字符");
    }
    return parts.join("/");
  }

  function setWorkspaceStatus(message, tone = "") {
    $("workspaceStatus").textContent = message;
    $("workspaceStatus").className = `operation-status small ${tone ? `activity-${tone}` : "muted"}`;
  }

  function renderWorkspace() {
    const loaded = state.workspaceLoaded && state.workspaceAppid === state.appid;
    const files = loaded ? state.workspaceFiles : [];
    const busy = Boolean(state.workspaceOperation);
    const disabled = busy || Boolean(state.appOperation) || !state.token || !state.appid;
    const entries = workspaceEntries();
    const search = $("workspaceSearch").value.trim().toLowerCase();
    const filtered = files.filter((file) => file.path.toLowerCase().includes(search));
    const pages = Math.max(1, Math.ceil(filtered.length / state.workspacePageSize));
    state.workspacePage = Math.max(1, Math.min(state.workspacePage, pages));
    $("workspaceAppId").textContent = state.appid || "未设置";
    $("workspaceSelection").textContent = entries.length
      ? `已选择 ${entries.length} 个文件，共 ${formatBytes(entries.reduce((total, entry) => total + entry.file.size, 0))}`
      : "尚未选择文件";
    ["workspaceFiles", "workspaceDirectory", "workspaceUploadPath", "workspaceRefreshButton"].forEach((id) => {
      $(id).disabled = disabled;
    });
    $("workspaceUploadButton").disabled = disabled || !entries.length;
    $("workspaceSearch").disabled = busy || !loaded;
    $("workspacePageSize").disabled = busy || !loaded;
    $("workspacePreviousButton").disabled = disabled || !loaded || state.workspacePage <= 1;
    $("workspaceNextButton").disabled = disabled || !loaded || state.workspacePage >= pages;
    $("workspacePageLabel").textContent = `第 ${state.workspacePage} / ${pages} 页`;
    $("workspaceSummary").textContent = loaded
      ? `共 ${files.length} 个文件 / ${formatBytes(state.workspaceTotalBytes)}${search ? `，匹配 ${filtered.length} 个` : ""}`
      : "文件列表尚未加载";
    $("workspaceFileList").setAttribute("aria-busy", String(busy));
    $("workspaceUploadForm").setAttribute("aria-busy", String(busy));
    const rows = $("workspaceFileRows");
    rows.replaceChildren();
    const offset = (state.workspacePage - 1) * state.workspacePageSize;
    const appid = state.appid;
    filtered.slice(offset, offset + state.workspacePageSize).forEach((file) => {
      const row = document.createElement("tr");
      const path = document.createElement("td");
      path.textContent = file.path;
      const size = document.createElement("td");
      size.textContent = formatBytes(file.size);
      size.title = `${file.size} bytes`;
      const hash = document.createElement("td");
      hash.textContent = file.sha256 ? `${file.sha256.slice(0, 12)}…` : "--";
      hash.title = file.sha256 || "";
      const actions = document.createElement("td");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "button button-quiet button-danger";
      remove.textContent = "删除";
      remove.setAttribute("aria-label", `删除 ${file.path}`);
      remove.disabled = disabled;
      remove.addEventListener("click", () => withFeedback(() => deleteWorkspaceFile(file.path, appid), null));
      actions.appendChild(remove);
      row.append(path, size, hash, actions);
      rows.appendChild(row);
    });
    $("workspaceEmpty").hidden = filtered.length > 0;
    $("workspaceEmpty").textContent = !state.token ? "请先连接控制端。"
      : !state.appid ? "请选择已注册应用。"
        : !loaded ? (busy ? "正在处理文件，请稍候…" : "请刷新文件列表。")
          : files.length ? "没有匹配的文件。" : "Workspace 为空，请上传文件后再创建构建。";
  }

  function resetWorkspaceView(message = "应用上下文已重置，请刷新文件列表或上传文件。") {
    state.workspaceOperation?.controller.abort();
    state.workspaceOperation = null;
    state.workspaceAppid = "";
    state.workspaceFiles = [];
    state.workspaceTotalBytes = 0;
    state.workspaceLoaded = false;
    state.workspacePage = 1;
    ["workspaceFiles", "workspaceDirectory", "workspaceUploadPath", "workspaceSearch"].forEach((id) => { $(id).value = ""; });
    setWorkspaceStatus(message);
    renderWorkspace();
  }

  function workspaceContextMatches(operation) {
    return state.workspaceOperation === operation && state.appid === operation.appid
      && Boolean(state.token) && !operation.controller.signal.aborted;
  }

  async function withWorkspaceOperation(label, action) {
    ensureAppIdle();
    if (!state.token) throw new Error("请先连接控制端");
    if (!state.appid) throw new Error("请先选择已注册应用");
    if (state.workspaceOperation) throw new Error("文件操作进行中，请等待完成");
    const operation = { appid: state.appid, controller: new AbortController(), changed: false };
    state.workspaceOperation = operation;
    setWorkspaceStatus(label);
    renderWorkspace();
    try {
      return await action(operation);
    } catch (error) {
      if (!workspaceContextMatches(operation) || error?.name === "AbortError") return null;
      if (operation.changed) {
        state.workspaceLoaded = false;
        state.workspaceFiles = [];
        state.workspaceTotalBytes = 0;
      }
      setWorkspaceStatus(`${operation.changed ? "文件变更已完成，但列表刷新失败" : "文件操作失败"}：${errorMessage(error)}。请刷新文件列表确认服务器上的文件状态。`, "error");
      throw error;
    } finally {
      if (state.workspaceOperation === operation) {
        state.workspaceOperation = null;
        renderWorkspace();
      }
    }
  }

  async function readWorkspace(operation) {
    const value = await request(`/api/v1/workspace/${encodeURIComponent(operation.appid)}`, { signal: operation.controller.signal });
    if (!workspaceContextMatches(operation)) return null;
    if (value?.appid !== operation.appid || !Array.isArray(value.files)) throw new Error("文件列表与当前应用不匹配");
    state.workspaceAppid = operation.appid;
    state.workspaceFiles = value.files;
    state.workspaceTotalBytes = value.total_bytes;
    state.workspaceLoaded = true;
    renderWorkspace();
    return value;
  }

  async function loadWorkspace() {
    return withWorkspaceOperation("正在加载文件列表…", async (operation) => {
      const value = await readWorkspace(operation);
      if (!value) return null;
      setWorkspaceStatus(value.files.length ? "文件列表已刷新。文件全部准备好后，请显式创建新的构建。" : "Workspace 为空：请先上传源码和配置，避免构建时报 Workspace mount source is empty。", value.files.length ? "success" : "");
      return value;
    });
  }

  function activateWorkspace() {
    if (state.activeSection !== "workspaceSection" || !state.token || !state.appid || state.appOperation || state.workspaceOperation) return;
    if (state.workspaceLoaded && state.workspaceAppid === state.appid) return;
    withFeedback(loadWorkspace, null);
  }

  async function uploadWorkspace() {
    const entries = workspaceEntries();
    if (!entries.length) throw new Error("请先选择要上传的文件或目录");
    const prefix = workspaceRelativePath($("workspaceUploadPath").value.trim(), true);
    const form = new FormData();
    const paths = new Set();
    if (prefix) form.append("path", prefix);
    entries.forEach((entry) => {
      const path = workspaceRelativePath(entry.path);
      const target = workspaceRelativePath(prefix ? `${prefix}/${path}` : path);
      if (paths.has(target)) throw new Error(`存在重复文件路径：${target}`);
      paths.add(target);
      form.append("files", entry.file, path);
    });
    return withWorkspaceOperation(`正在上传 ${entries.length} 个文件，请稍候…`, async (operation) => {
      await request(`/api/v1/workspace/${encodeURIComponent(operation.appid)}`, { method: "POST", body: form, signal: operation.controller.signal });
      if (!workspaceContextMatches(operation)) return null;
      operation.changed = true;
      $("workspaceFiles").value = "";
      $("workspaceDirectory").value = "";
      const value = await readWorkspace(operation);
      if (!value) return null;
      setWorkspaceStatus(`已上传 ${entries.length} 个文件，列表已刷新。补全文件后请创建新的构建；此前的失败构建和告警不会因此自动变为成功。`, "success");
      return value;
    });
  }

  async function deleteWorkspaceFile(path, appid) {
    if (appid !== state.appid) return null;
    if (!window.confirm(`删除应用 ${appid} 的暂存文件 ${path}？\n\n仅删除此文件，不会修改已有构建快照或运行实例。删除后如需发布变更，请创建新的构建。`)) return null;
    return withWorkspaceOperation(`正在删除 ${path}…`, async (operation) => {
      await request(`/api/v1/workspace/${encodeURIComponent(appid)}?${new URLSearchParams({ path })}`, { method: "DELETE", signal: operation.controller.signal });
      if (!workspaceContextMatches(operation)) return null;
      operation.changed = true;
      const value = await readWorkspace(operation);
      if (!value) return null;
      setWorkspaceStatus(`已删除 ${path}，列表已刷新。${value.files.length ? "已有构建快照和运行实例不受影响。" : "Workspace 已为空，请上传文件后再创建构建。"}`, "success");
      return value;
    });
  }

  async function loadAppChoices() {
    const selector = $("contextAppSelect");
    if (!selector) return [];
    const apps = await request("/api/v1/jenkins/app_list");
    selector.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = apps.length ? "请选择应用" : "暂无已创建应用";
    selector.appendChild(placeholder);
    apps.forEach((app) => {
      const option = document.createElement("option");
      option.value = app.appid;
      option.textContent = `${app.name} (${app.appid})`;
      selector.appendChild(option);
    });
    selector.value = apps.some((app) => app.appid === state.appid) ? state.appid : "";
    $("contextAppHint").textContent = apps.length
      ? `已加载 ${apps.length} 个应用，选择后会同步资源查询上下文。`
      : "暂无已创建应用，请先在应用页面创建。";
    return apps;
  }

  async function selectContextApp() {
    ensureAppIdle();
    const appid = $("contextAppSelect").value.trim();
    if (!appid) throw new Error("请先选择已有应用");
    return withAppOperation("读取应用中…", appid, async (operation) => {
      const value = await request(`/api/v1/jenkins/app_info/${encodeURIComponent(appid)}`);
      if (state.appContextVersion !== operation.version || $("contextAppSelect").value !== appid
        || state.appid !== operation.activeAppid || $("appId").value.trim() !== operation.inputAppid) return null;
      applyApp(value);
      return value;
    });
  }

  function applyBuild(build, appid = build.appid || state.buildAppid || state.appid) {
    state.buildId = build.build_id || state.buildId;
    state.buildAppid = appid;
    state.latestBuild = build;
    $("buildId").value = state.buildId;
    $("metricBuild").textContent = build.status || "--";
    $("buildStatus").textContent = build.status || "--";
    $("buildStatus").className = `status-pill ${statusClass(build.status)}`;
    $("jenkinsBuild").textContent = build.jenkins_build_number ?? "--";
    $("celeryTask").textContent = build.celery_task_id || "--";
    $("buildCommit").textContent = build.git_commit_sha || "--";
  }

  function resetBuildView({ keepInput = false } = {}) {
    stopHistoryDetailLoading();
    state.latestBuild = null;
    state.buildAppid = "";
    state.buildLastRefreshedAt = "";
    state.buildId = keepInput ? $("buildId").value.trim() : "";
    if (!keepInput) $("buildId").value = "";
    ["metricBuild", "buildStatus", "jenkinsBuild", "celeryTask", "buildCommit"].forEach((id) => { $(id).textContent = "--"; });
    $("buildStatus").className = "status-pill neutral";
    $("historyDetailPanel").hidden = true;
    pretty("buildOutput", "尚未查询当前构建。");
  }

  function markBuildStale(label = "未刷新") {
    if (!state.latestBuild) return;
    const status = state.latestBuild.status || "未知";
    $("metricBuild").textContent = `${status}（${label}）`;
    $("buildStatus").textContent = `${status}（${label}）`;
  }

  function isTerminalBuild(build) {
    return ["succeeded", "failed", "cancelled"].includes(String(build?.status || "").toLowerCase());
  }

  function validateBuildResponse(build, buildId, appid) {
    if (build?.build_id !== buildId) throw new Error("返回的 Build ID 与查询不匹配");
    if (Object.prototype.hasOwnProperty.call(build, "appid")) {
      if (typeof build.appid !== "string" || !build.appid.trim() || (appid !== undefined && build.appid !== appid)) {
        throw new Error("返回的 App ID 与查询上下文不匹配");
      }
      return build.appid;
    }
    return appid;
  }

  function updateBuildSubmitState() {
    const form = $("buildForm");
    const button = form.querySelector('button[type="submit"]');
    button.disabled = Boolean(state.buildCreateOperation || state.appOperation);
    button.textContent = state.buildCreateOperation ? "提交中，请稍候…" : "提交构建";
    if (state.buildCreateOperation) form.setAttribute("aria-busy", "true");
    else form.removeAttribute("aria-busy");
  }

  function stopBuildCreation() {
    const operation = state.buildCreateOperation;
    if (!operation) return;
    state.buildCreateOperation = null;
    operation.controller.abort();
    updateBuildSubmitState();
  }

  function setBuildPollStatus(message, tone = "muted") {
    const target = $("buildPollStatus");
    if (!target) return;
    target.textContent = message;
    target.className = `operation-status small ${tone === "muted" ? "muted" : `activity-${tone}`}`;
  }

  function stopBuildPolling(message = "自动状态查询已停止。", tone = "muted") {
    state.buildPollGeneration += 1;
    if (state.buildPollTimer !== null) {
      window.clearTimeout(state.buildPollTimer);
      state.buildPollTimer = null;
    }
    const controller = state.buildPollController;
    state.buildPollController = null;
    if (controller) controller.abort();
    setBuildPollStatus(message, tone);
    return state.buildPollGeneration;
  }

  function buildContextMatches(generation, buildId, appContext, appInput, { inspect = false } = {}) {
    return generation === state.buildPollGeneration
      && Boolean(state.token)
      && state.appid === appContext
      && $("appId").value.trim() === appInput
      && (inspect
        ? $("historyDetailTitle").textContent === `任务详情：${buildId}`
        : $("buildId").value.trim() === buildId);
  }

  function scheduleBuildPoll(generation, buildId, appContext, appInput, delay, expectedAppid = appInput || appContext) {
    if (generation !== state.buildPollGeneration || !state.token) return;
    state.buildPollTimer = window.setTimeout(
      () => pollBuildStatus(generation, buildId, appContext, appInput, expectedAppid),
      delay,
    );
  }

  async function pollBuildStatus(generation, buildId, appContext, appInput, expectedAppid) {
    if (!buildContextMatches(generation, buildId, appContext, appInput)) return;
    const appContextVersion = state.appContextVersion;
    state.buildPollTimer = null;
    const controller = new AbortController();
    state.buildPollController = controller;
    try {
      const value = await request(
        `/api/v1/jenkins/build_info/${encodeURIComponent(buildId)}`,
        { signal: controller.signal },
        true,
        true,
      );
      if (!buildContextMatches(generation, buildId, appContext, appInput) || state.appContextVersion !== appContextVersion) return;
      validateBuildResponse(value, buildId, expectedAppid);
      state.buildPollFailures = 0;
      applyBuild(value, expectedAppid);
      pretty("buildOutput", value);
      const refreshedAt = formatTime();
      state.buildLastRefreshedAt = refreshedAt;
      if (!$("historyDetailPanel").hidden && $("historyDetailTitle").textContent === `任务详情：${buildId}`) {
        pretty("historyDetailBuild", value);
        if (historyDetailContextMatches(state.historyDetailOperation)) {
          state.historyDetailOperation.buildPending = false;
          state.historyDetailOperation.buildError = "";
          updateHistoryDetailStatus(state.historyDetailOperation);
        }
      }
      if (isTerminalBuild(value)) {
        stopBuildPolling(`最近刷新：${refreshedAt}；终态 ${value.status}，自动查询已停止。${value.error ? ` 原因：${value.error}` : ""}`, value.status === "succeeded" ? "success" : "error");
      } else {
        setBuildPollStatus(`最近刷新：${refreshedAt}；当前状态 ${value.status || "未知"}，约 5 秒后再次查询。`);
        scheduleBuildPoll(generation, buildId, appContext, appInput, BUILD_POLL_INTERVAL_MS, expectedAppid);
      }
    } catch (error) {
      if (!buildContextMatches(generation, buildId, appContext, appInput) || state.appContextVersion !== appContextVersion) return;
      if (!state.token || (error instanceof ApiError && [401, 403].includes(error.status))) {
        stopBuildPolling("认证或权限验证失败，状态未刷新；自动查询已停止，请重新连接。", "error");
        return;
      }
      state.buildPollFailures += 1;
      const retryIn = Math.min(BUILD_POLL_INTERVAL_MS * (2 ** (state.buildPollFailures - 1)), BUILD_POLL_MAX_RETRY_MS);
      const reason = errorMessage(error);
      markBuildStale();
      if (historyDetailContextMatches(state.historyDetailOperation)) {
        state.historyDetailOperation.buildError = reason;
        updateHistoryDetailStatus(state.historyDetailOperation);
      }
      setBuildPollStatus(`状态未刷新（${reason}）；${state.buildLastRefreshedAt ? `最近刷新：${state.buildLastRefreshedAt}` : "尚未取得实时查询结果"}。下方仍是上次结果，约 ${Math.round(retryIn / 1000)} 秒后重试，也可点击“再次查询”。`, "error");
      scheduleBuildPoll(generation, buildId, appContext, appInput, retryIn, expectedAppid);
    } finally {
      if (state.buildPollController === controller) state.buildPollController = null;
    }
  }

  function startBuildPolling(build, { accepted = false, inspect = false, appid = $("appId").value.trim() || state.appid } = {}) {
    const generation = stopBuildPolling("准备查询构建状态...");
    if (!accepted) state.buildLastRefreshedAt = formatTime();
    if (isTerminalBuild(build)) {
      setBuildPollStatus(`最近刷新：${state.buildLastRefreshedAt}；状态 ${build.status} 为终态，自动查询已停止。${build.error ? ` 原因：${build.error}` : ""}`, build.status === "succeeded" ? "success" : "error");
      return;
    }
    if (!state.token) {
      setBuildPollStatus("未登录，无法自动查询构建状态。", "error");
      return;
    }
    const buildId = build.build_id;
    const appContext = state.appid;
    const appInput = $("appId").value.trim();
    state.buildPollFailures = 0;
    if (accepted) {
      state.buildLastRefreshedAt = "";
      markBuildStale("受理回执");
      setBuildPollStatus("HTTP 202 仅表示任务已受理，不代表构建完成；当前显示受理快照，约 5 秒后查询最新状态。", "success");
    } else if (inspect) {
      setBuildPollStatus(`最近刷新：${state.buildLastRefreshedAt}；任务详情已加载，约 5 秒后再次查询状态。`, "muted");
    } else {
      setBuildPollStatus(`最近刷新：${state.buildLastRefreshedAt}；约 5 秒后再次查询状态。`, "muted");
    }
    scheduleBuildPoll(generation, buildId, appContext, appInput, BUILD_POLL_INTERVAL_MS, appid);
  }

  async function refreshAccessToken(signal) {
    if (!state.refreshToken) throw new Error("没有可用的刷新令牌");
    const response = await fetch("/oauth2/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: state.refreshToken }),
      signal,
    });
    const text = await response.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = text; }
    }
    if (signal?.aborted) throw new DOMException("Request aborted", "AbortError");
    if (!response.ok) throw new ApiError(response.status, payload);
    state.token = payload.access_token;
    state.refreshToken = payload.refresh_token || state.refreshToken;
    localStorage.setItem(TOKEN_KEY, state.token);
    localStorage.setItem(REFRESH_TOKEN_KEY, state.refreshToken);
    return payload;
  }

  async function request(path, options = {}, allowRefresh = true, quiet = false) {
    try {
      const headers = new Headers(options.headers || {});
      const requestOptions = { ...options };
      if (options.body !== undefined && !(options.body instanceof FormData)) {
        headers.set("Content-Type", "application/json");
        requestOptions.body = JSON.stringify(options.body);
      }
      if (state.token && !path.startsWith("/oauth2/")) headers.set("Authorization", `Bearer ${state.token}`);
      const response = await fetch(path, { ...requestOptions, headers });
      const text = await response.text();
      let payload = null;
      if (text) {
        try { payload = JSON.parse(text); } catch { payload = text; }
      }
      if (options.signal?.aborted) throw new DOMException("Request aborted", "AbortError");
      if (!response.ok) {
        if (response.status === 401 && allowRefresh && !path.startsWith("/oauth2/") && state.refreshToken) {
          try {
            await refreshAccessToken(options.signal);
            return request(path, options, false, quiet);
          } catch {
            // Fall through and report the original unauthorized response.
          }
        }
        if (options.signal?.aborted) throw new DOMException("Request aborted", "AbortError");
        if (response.status === 401 && !path.startsWith("/oauth2/")) {
          state.token = "";
          state.refreshToken = "";
          localStorage.removeItem(TOKEN_KEY);
          localStorage.removeItem(REFRESH_TOKEN_KEY);
          stopHistoryPolling();
          stopBuildCreation();
          stopHistoryDetailLoading();
          resetWorkspaceView("访问令牌已失效，请重新连接后刷新文件列表。");
          resetBuildResources();
          markBuildStale();
          stopBuildPolling("访问令牌失效，自动查询已停止，请重新连接。", "error");
          setSession(false, "访问令牌已失效，请重新连接。");
        }
        throw new ApiError(response.status, payload);
      }
      return payload;
    } catch (error) {
      if (!quiet && error?.name !== "AbortError") notifyError(error);
      throw error;
    }
  }

  async function withFeedback(action, outputId) {
    try {
      const value = await action();
      if (value === null || value === undefined) return null;
      if (outputId) pretty(outputId, value);
      setActivity("请求完成", "success");
      return value;
    } catch (error) {
      const message = notifyError(error);
      if (outputId) pretty(outputId, { error: message });
      setActivity(message, "error");
      return null;
    }
  }

  async function connect() {
    const payload = await request("/oauth2/token", {
      method: "POST",
      body: { client_id: $("workerName").value.trim(), client_secret: $("workerSecret").value },
    });
    state.token = payload.access_token;
    state.refreshToken = payload.refresh_token || "";
    localStorage.setItem(TOKEN_KEY, state.token);
    if (state.refreshToken) localStorage.setItem(REFRESH_TOKEN_KEY, state.refreshToken);
    setSession(true, `访问令牌有效 ${payload.expires_in || "未知"} 秒`);
    activateWorkspace();
    closeModal("connectionModal");
    await Promise.all([loadTemplates(), loadBaseImages(), loadHistory({ resetPage: true, silent: true })]);
    updateHistoryPolling();
    return payload;
  }

  function sameJson(left, right) {
    if (left === right) return true;
    if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
    if (Array.isArray(left) !== Array.isArray(right)) return false;
    const leftKeys = Object.keys(left).sort();
    const rightKeys = Object.keys(right).sort();
    return leftKeys.length === rightKeys.length
      && leftKeys.every((key, index) => key === rightKeys[index] && sameJson(left[key], right[key]));
  }

  function appPayload({ patch = false } = {}) {
    const appid = appidFromInput();
    const components = $("components").value.split(",").map((item) => item.trim()).filter(Boolean);
    const payload = {
      appid,
      name: $("appName").value.trim(),
      repository_url: $("repositoryUrl").value.trim(),
      git_ref: $("gitRef").value.trim() || "main",
      components,
    };
    const environmentText = $("environment").value;
    if (!patch || (environmentText.trim() && (state.environmentDirty || environmentText !== state.environmentBaseline))) {
      const environment = parsedJson(environmentText.trim() ? environmentText : "{}", "环境变量", false);
      if (!environment || typeof environment !== "object" || Array.isArray(environment)) {
        throw new Error("环境变量必须是 JSON 对象；明确填写 {} 可清空已有变量");
      }
      payload.environment = environment;
    }
    const compose = composeJson();
    if (compose !== undefined) payload.compose = compose;
    if (patch && state.loadedApp?.appid === appid) {
      ["name", "repository_url", "git_ref", "components", "compose"].forEach((key) => {
        if (Object.prototype.hasOwnProperty.call(payload, key) && sameJson(payload[key], state.loadedApp[key])) {
          delete payload[key];
        }
      });
    }
    return payload;
  }

  async function createApp() {
    ensureAppIdle();
    const payload = appPayload();
    return withAppOperation("创建应用中…", payload.appid, async (operation) => {
      const value = await request("/api/v1/jenkins/app_create", { method: "POST", body: payload });
      if (!sameAppContext(payload.appid, operation.version)) return null;
      applyApp(value);
      pretty("appOutput", value);
      return value;
    });
  }

  async function getApp() {
    ensureAppIdle();
    const appid = appidFromInput();
    return withAppOperation("读取应用中…", appid, async (operation) => {
      const value = await request(`/api/v1/jenkins/app_info/${encodeURIComponent(appid)}`);
      if (!sameAppContext(appid, operation.version)) return null;
      applyApp(value);
      pretty("appOutput", value);
      return value;
    });
  }

  async function patchApp() {
    ensureAppIdle();
    const payload = appPayload({ patch: true });
    const appid = payload.appid;
    delete payload.appid;
    return withAppOperation("更新应用中…", appid, async (operation) => {
      const value = await request(`/api/v1/jenkins/app_info/${encodeURIComponent(appid)}`, { method: "PATCH", body: payload });
      if (!sameAppContext(appid, operation.version)) return null;
      applyApp(value);
      pretty("appOutput", value);
      return value;
    });
  }

  function renderSyncResult(result) {
    $("syncResultPanel").hidden = false;
    $("syncComposeChanged").textContent = result.compose_changed ? "已变更" : "未变更";
    $("syncCommitChanged").textContent = result.commit_changed ? "已变更" : "未变更";
    $("syncGitRef").textContent = result.git_ref || "--";
    $("syncComposeFile").textContent = result.compose_file || "--";
    $("syncCommitId").textContent = result.commit_id || "--";
    $("syncPreviousCommitId").textContent = result.previous_commit_id || "无之前的仓库 Commit";
    $("syncUpdatedAt").textContent = formatDate(result.updated_at);
    pretty("appSyncOutput", result);
  }

  async function syncApp() {
    ensureAppIdle();
    const appid = appidFromInput();
    const saved = state.loadedApp?.appid === appid ? state.loadedApp : null;
    const refDescription = saved ? `已保存的 Git 引用 ${saved.git_ref}` : "服务端已保存的 Git 引用";
    if (!window.confirm(`从应用 ${appid} 的已保存 GitLab 仓库（${refDescription}）同步 Compose？\n\n这会完整克隆仓库并覆盖应用已保存的 Compose，包括手动配置。成功后重新读取应用会替换编辑器草稿，以及尚未保存的仓库地址、Git 引用和其他表单修改；当前编辑器的仓库和引用不会用于同步。\n\n此操作不会自动触发构建。`)) return null;
    return withAppOperation("同步中，请稍候…", appid, async (operation) => {
      const started = Date.now();
      const showProgress = () => {
        if (!sameAppContext(appid, operation.version)) return;
        const seconds = Math.floor((Date.now() - started) / 1000);
        setSyncStatus(`正在完整克隆仓库并同步 ${appid}（${refDescription}），已等待 ${seconds} 秒。大仓库可能需要 600 秒或更久，请勿重复提交；不会自动构建。`);
      };
      $("syncResultPanel").hidden = true;
      ["syncComposeChanged", "syncCommitChanged", "syncGitRef", "syncComposeFile", "syncCommitId", "syncPreviousCommitId", "syncUpdatedAt"].forEach((id) => { $(id).textContent = "--"; });
      pretty("appSyncOutput", "正在同步仓库，请稍候…");
      showProgress();
      setActivity(`正在同步应用 ${appid}，请稍候`);
      operation.timer = window.setInterval(showProgress, 1000);
      let result;
      try {
        result = await request(`/api/v1/jenkins/app_sync/${encodeURIComponent(appid)}`, { method: "POST" });
      } catch (error) {
        window.clearInterval(operation.timer);
        if (sameAppContext(appid, operation.version)) {
          $("syncResultPanel").hidden = false;
          pretty("appSyncOutput", { error: errorMessage(error), ...(error instanceof ApiError ? { status: error.status, response: error.payload } : {}) });
          setSyncStatus(`同步请求失败：${errorMessage(error)}。未替换编辑器和当前来源显示；若网络中断，服务端可能已完成操作，可先点击“读取应用”确认。`, "error");
          setActivity(`应用同步失败：${errorMessage(error)}`, "error");
        }
        return null;
      }
      window.clearInterval(operation.timer);
      if (!sameAppContext(appid, operation.version)) return null;
      renderSyncResult(result);
      state.syncRefreshPendingAppid = appid;
      setSyncStatus("仓库同步成功，正在重新读取应用以确认已保存的 Compose 和来源…");
      try {
        const app = await request(`/api/v1/jenkins/app_info/${encodeURIComponent(appid)}`);
        if (!sameAppContext(appid, operation.version)) return null;
        applyApp(app);
        pretty("appOutput", app);
      } catch (error) {
        if (sameAppContext(appid, operation.version)) {
          setSyncStatus(`仓库同步成功，但应用刷新失败（读取应用失败）：${errorMessage(error)}。编辑器和来源仍是同步前的显示；下方保留完整同步响应，请点击“读取应用”重试刷新，无需再次同步。未触发构建。`, "error");
          setActivity("仓库同步成功，但应用刷新失败；请重试读取应用", "error");
        }
        return result;
      }
      const changes = !result.compose_changed && !result.commit_changed
        ? "Compose 和 Commit 均未更改"
        : `Compose ${result.compose_changed ? "已更改" : "未更改"}，Commit ${result.commit_changed ? "已更改" : "未更改"}`;
      setSyncStatus(`仓库同步成功：${changes}。已重新读取保存的配置；未自动触发构建，确认后请显式提交构建。`, "success");
      setActivity(`仓库同步成功：${changes}；未触发构建`, "success");
      return result;
    });
  }

  async function createBuild() {
    if (state.buildCreateOperation) return null;
    ensureAppIdle();
    const appid = appidFromInput();
    if (state.syncRefreshPendingAppid === appid) {
      throw new Error("仓库同步已成功，但应用配置尚未重新读取。请先点击“读取应用”成功刷新配置和来源，再提交构建。");
    }
    const gitRef = $("buildGitRef").value.trim();
    const body = gitRef ? { git_ref: gitRef } : {};
    const generation = stopBuildPolling("正在提交构建请求...");
    resetBuildView();
    const appContext = state.appid;
    const appInput = $("appId").value.trim();
    const appContextVersion = state.appContextVersion;
    const controller = new AbortController();
    const operation = { appid, controller };
    state.buildCreateOperation = operation;
    updateBuildSubmitState();
    try {
      const value = await request(`/api/v1/jenkins/build_create/${encodeURIComponent(appid)}`, { method: "POST", body, signal: controller.signal }, true, true);
      if (state.buildCreateOperation !== operation || generation !== state.buildPollGeneration || !state.token || state.appid !== appContext || $("appId").value.trim() !== appInput || state.appContextVersion !== appContextVersion) return null;
      if (value?.appid !== appid) throw new Error("返回的 App ID 与提交不匹配");
      applyBuild(value, appid);
      pretty("buildOutput", value);
      startBuildPolling(value, { accepted: true, appid });
      loadHistory({ resetPage: true, silent: true }).catch(() => {});
      return value;
    } catch (error) {
      if (state.buildCreateOperation !== operation || generation !== state.buildPollGeneration || state.appContextVersion !== appContextVersion || error?.name === "AbortError") return null;
      setBuildPollStatus(`提交请求失败：${errorMessage(error)}；未收到受理回执。`, "error");
      throw error;
    } finally {
      if (state.buildCreateOperation === operation) {
        state.buildCreateOperation = null;
        updateBuildSubmitState();
      }
    }
  }

  async function getBuild() {
    const buildId = $("buildId").value.trim() || state.buildId;
    if (!buildId) throw new Error("请先填写 Build ID");
    if (state.buildPollController && state.buildId === buildId) return null;
    state.buildId = buildId;
    const generation = stopBuildPolling("正在查询最新构建状态...");
    const appContext = state.appid;
    const appInput = $("appId").value.trim();
    const appContextVersion = state.appContextVersion;
    const expectedAppid = historyDetailContextMatches(state.historyDetailOperation)
      ? state.historyDetailOperation.appid
      : state.latestBuild?.build_id === buildId ? state.buildAppid : undefined;
    const fallbackAppid = appInput || appContext;
    const controller = new AbortController();
    state.buildPollController = controller;
    try {
      const value = await request(`/api/v1/jenkins/build_info/${encodeURIComponent(buildId)}`, { signal: controller.signal }, true, true);
      if (!buildContextMatches(generation, buildId, appContext, appInput) || state.appContextVersion !== appContextVersion) return null;
      // A new free-form ID can belong to another app. Bind its validated
      // owner once; all later queries/polls must keep that identity.
      const buildAppid = validateBuildResponse(value, buildId, expectedAppid) ?? fallbackAppid;
      applyBuild(value, buildAppid);
      pretty("buildOutput", value);
      startBuildPolling(value, { appid: buildAppid });
      return value;
    } catch (error) {
      if (!buildContextMatches(generation, buildId, appContext, appInput) || state.appContextVersion !== appContextVersion || error?.name === "AbortError") return null;
      markBuildStale();
      if (error instanceof ApiError && [401, 403].includes(error.status)) {
        stopBuildPolling("认证或权限验证失败，状态未刷新；自动查询已停止，请重新连接。", "error");
        return null;
      }
      state.buildPollFailures = 1;
      setBuildPollStatus(`状态未刷新（${errorMessage(error)}）；约 5 秒后重试，也可点击“再次查询”。下方不是当前实时状态。`, "error");
      scheduleBuildPoll(generation, buildId, appContext, appInput, BUILD_POLL_INTERVAL_MS, expectedAppid ?? fallbackAppid);
      return null;
    } finally {
      if (state.buildPollController === controller) state.buildPollController = null;
    }
  }

  function historyPageCount() {
    return Math.max(1, Math.ceil(state.historyTotal / state.historyPageSize));
  }

  function renderHistoryPagination() {
    const pageCount = historyPageCount();
    $("historyPageInfo").textContent = `第 ${state.historyPage} / ${pageCount} 页`;
    $("historyPreviousButton").disabled = state.historyPage <= 1;
    $("historyNextButton").disabled = state.historyPage >= pageCount;
  }

  function textCell(row, value, className = "") {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    cell.textContent = value == null || value === "" ? "--" : String(value);
    row.appendChild(cell);
    return cell;
  }

  function renderHistoryRows(items) {
    const body = $("historyTableBody");
    body.replaceChildren();
    $("historyTable").hidden = items.length === 0;
    $("historyEmpty").hidden = items.length !== 0;

    items.forEach((build) => {
      const row = document.createElement("tr");
      textCell(row, build.build_id);
      textCell(row, build.appid);
      const statusCell = document.createElement("td");
      const status = document.createElement("span");
      status.className = `status-pill ${statusClass(build.status)}`;
      status.textContent = build.status || "--";
      statusCell.appendChild(status);
      row.appendChild(statusCell);
      textCell(row, build.git_ref);
      textCell(row, build.jenkins_build_number ?? "--");
      textCell(row, formatDate(build.created_at));

      const actions = document.createElement("td");
      const detailButton = document.createElement("button");
      detailButton.type = "button";
      detailButton.className = "button button-quiet history-detail-button";
      detailButton.textContent = "查看详情";
      detailButton.addEventListener("click", () => {
        withFeedback(() => inspectBuild(build.build_id, build.appid), null);
      });
      actions.appendChild(detailButton);
      row.appendChild(actions);
      body.appendChild(row);
    });
  }

  function historyFilterQuery() {
    const params = new URLSearchParams();
    const appid = $("historyAppId").value.trim();
    const status = $("historyStatus").value;
    state.historyPageSize = Number($("historyPageSize").value) || 20;
    if (appid) params.set("app_id", appid);
    if (status) params.set("status", status);
    params.set("page", String(state.historyPage));
    params.set("page_size", String(state.historyPageSize));
    return params;
  }

  async function loadHistory({ resetPage = false, silent = false } = {}) {
    if (!state.token) throw new Error("请先连接控制端");
    if (resetPage) state.historyPage = 1;
    state.historyLoading = true;
    $("historyLoading").textContent = "加载中...";
    try {
      const params = historyFilterQuery();
      const value = await request(`/api/v1/jenkins/build_list?${params.toString()}`);
      const items = Array.isArray(value?.items) ? value.items : [];
      state.historyPage = Math.max(1, Number(value?.page) || state.historyPage);
      state.historyPageSize = Math.max(1, Number(value?.page_size) || state.historyPageSize);
      state.historyTotal = Math.max(0, Number(value?.total) || 0);
      renderHistoryRows(items);
      renderHistoryPagination();
      const appid = $("historyAppId").value.trim();
      const status = $("historyStatus").value;
      const filterText = [appid && `App ID ${appid}`, status && `状态 ${status}`].filter(Boolean).join("，");
      $("historySummary").textContent = filterText
        ? `${filterText}，共 ${state.historyTotal} 个任务`
        : `共 ${state.historyTotal} 个任务`;
      if (!silent) setActivity(`已加载 ${items.length} 个历史任务`, "success");
      return value;
    } finally {
      state.historyLoading = false;
      $("historyLoading").textContent = "";
    }
  }

  function detailError(reason) {
    return { error: reason instanceof Error ? reason.message : String(reason) };
  }

  function accessServices(value) {
    if (Array.isArray(value)) return value;
    return Array.isArray(value?.services) ? value.services : [];
  }

  function accessUrls(service) {
    if (!Array.isArray(service?.access_urls)) return [];
    return service.access_urls.filter((url) => typeof url === "string" && /^https?:\/\//i.test(url));
  }

  function accessReason(service, urls) {
    const knownReasons = {
      "No published ports": "未发布端口，无法从外部访问。请在 Compose 中配置 ports，例如 18082:3000，然后重新构建部署。",
      "No TCP published ports": "没有可用于 HTTP 访问的 TCP published 端口。",
      "Public host is not configured": "未配置公共主机，无法生成外部访问 URL。",
    };
    const reason = String(service?.access_reason || "").trim();
    return knownReasons[reason] || reason || (urls.length
      ? "可从配置的公共地址访问。"
      : "未发布端口，无法从外部访问。请在 Compose 中配置 ports，例如 18082:3000，然后重新构建部署。");
  }

  function renderServiceAccess(value) {
    const services = accessServices(value);
    $("serviceAccessRegion").textContent = value?.region || "--";
    const body = $("serviceAccessTableBody");
    const table = $("serviceAccessTable");
    const empty = $("serviceAccessEmpty");
    const summary = $("serviceAccessSummary");
    body.replaceChildren();
    table.hidden = services.length === 0;
    empty.hidden = services.length !== 0;

    let available = 0;
    services.forEach((service) => {
      const row = document.createElement("tr");
      textCell(row, service.build_id || "--");
      textCell(row, service.service_name || "--");
      textCell(row, service.image || "--");

      const statusCell = document.createElement("td");
      const status = document.createElement("span");
      status.className = `status-pill ${statusClass(service.status)}`;
      status.textContent = service.status || "--";
      statusCell.appendChild(status);
      row.appendChild(statusCell);

      const ports = Array.isArray(service.published_ports)
        ? service.published_ports.map((port) => {
          if (port && typeof port === "object") return port.published_port ?? port.published ?? "--";
          return port;
        }).filter((port) => port !== null && port !== undefined && port !== "")
        : [];
      textCell(row, ports.length ? ports.join(", ") : "--");

      const urls = accessUrls(service);
      if (urls.length) available += 1;
      const urlCell = document.createElement("td");
      if (urls.length) {
        urls.forEach((url, index) => {
          if (index) urlCell.appendChild(document.createElement("br"));
          const link = document.createElement("a");
          link.href = url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = url;
          urlCell.appendChild(link);
        });
      } else {
        urlCell.textContent = "--";
        urlCell.className = "muted";
      }
      row.appendChild(urlCell);

      const reasonCell = document.createElement("td");
      reasonCell.className = "access-reason";
      reasonCell.textContent = accessReason(service, urls);
      row.appendChild(reasonCell);
      body.appendChild(row);
    });

    if (!services.length) {
      empty.textContent = value?.access_reason || "当前应用没有已部署服务，或尚未查询访问入口。";
      summary.textContent = "尚未获得服务访问信息。";
      return value;
    }
    const total = services.length;
    summary.textContent = `${available}/${total} 个服务有可访问入口。`;
    if (value?.access_reason && available === 0) summary.textContent += ` ${value.access_reason}`;
    return value;
  }

  function renderDetailResult(targetId, result) {
    const target = $(targetId);
    prettyNode(target, result.status === "fulfilled" ? result.value : detailError(result.reason));
  }

  function resourcesForBuild(value, buildId) {
    if (!Array.isArray(value)) return value;
    return value.filter((item) => !item?.build_id || item.build_id === buildId);
  }

  function stopHistoryDetailLoading() {
    const operation = state.historyDetailOperation;
    state.historyDetailOperation = null;
    if (operation) operation.controller.abort();
  }

  function historyDetailContextMatches(operation) {
    return Boolean(operation)
      && state.historyDetailOperation === operation
      && Boolean(state.token)
      && state.appid === operation.appContext
      && state.appContextVersion === operation.appContextVersion
      && $("appId").value.trim() === operation.appInput
      && $("buildId").value.trim() === operation.buildId
      && !$("historyDetailPanel").hidden
      && $("historyDetailTitle").textContent === `任务详情：${operation.buildId}`;
  }

  function updateHistoryDetailStatus(operation) {
    if (!historyDetailContextMatches(operation)) return;
    const buildStatus = operation.buildPending
      ? "正在加载构建状态"
      : operation.buildError
        ? `构建状态未刷新（${operation.buildError}）`
        : `构建状态已刷新：${state.buildLastRefreshedAt}`;
    const resourcesStatus = operation.resourcePending
      ? `关联资源仍在加载（${operation.resourcePending} 个）`
      : operation.resourceFailures
        ? `关联资源已加载，${operation.resourceFailures} 个接口返回失败`
        : "关联资源已加载";
    $("historyDetailStatus").textContent = `${buildStatus}；${resourcesStatus}。`;
  }

  async function loadHistoryDetailResources(operation) {
    const resources = [
      ["events", "historyDetailEvents"],
      ["images", "historyDetailImages"],
      ["services", "historyDetailServices"],
      ["alerts", "historyDetailAlerts"],
    ];
    await Promise.allSettled(resources.map(async ([resource, target]) => {
      let result;
      try {
        const value = await request(`/api/v1/jenkins/app_${resource}/${encodeURIComponent(operation.appid)}`, { signal: operation.controller.signal }, true, true);
        result = { status: "fulfilled", value: resourcesForBuild(value, operation.buildId) };
      } catch (reason) {
        result = { status: "rejected", reason };
      }
      if (!historyDetailContextMatches(operation)) return;
      renderDetailResult(target, result);
      operation.resourcePending -= 1;
      if (result.status === "rejected") operation.resourceFailures += 1;
      updateHistoryDetailStatus(operation);
    }));
  }

  async function inspectBuild(buildId, appid) {
    if (!buildId) throw new Error("缺少 Build ID");
    const selectedAppid = appid || state.appid || $("historyAppId").value.trim();
    if (!selectedAppid) throw new Error("该任务没有可用的 App ID");
    const generation = stopBuildPolling("正在加载任务详情...");
    resetBuildView();
    const appContext = state.appid;
    const appInput = $("appId").value.trim();
    const controller = new AbortController();
    state.buildPollController = controller;
    const operation = {
      buildId,
      appid: selectedAppid,
      appContext,
      appInput,
      appContextVersion: state.appContextVersion,
      controller: new AbortController(),
      buildPending: true,
      buildError: "",
      resourcePending: 4,
      resourceFailures: 0,
    };
    state.historyDetailOperation = operation;
    state.buildId = buildId;
    $("buildId").value = buildId;
    resetBuildResources();
    $("buildResourcesId").value = buildId;
    $("historyDetailPanel").hidden = false;
    $("historyDetailTitle").textContent = `任务详情：${buildId}`;
    $("historyDetailMeta").textContent = `App ID：${selectedAppid}`;
    $("historyDetailStatus").textContent = "正在并行加载任务及关联资源...";
    ["historyDetailBuild", "historyDetailEvents", "historyDetailImages", "historyDetailServices", "historyDetailAlerts"].forEach((id) => {
      $(id).textContent = "加载中...";
    });

    // Resource requests have their own lifetime: starting/stopping the build
    // poll must not delay them or allow them to reapply an old BuildJob.
    loadHistoryDetailResources(operation);
    try {
      const value = await request(`/api/v1/jenkins/build_info/${encodeURIComponent(buildId)}`, { signal: controller.signal }, true, true);
      if (!buildContextMatches(generation, buildId, appContext, appInput, { inspect: true }) || !historyDetailContextMatches(operation)) return null;
      validateBuildResponse(value, buildId, selectedAppid);
      operation.buildPending = false;
      renderDetailResult("historyDetailBuild", { status: "fulfilled", value });
      applyBuild(value, selectedAppid);
      pretty("buildOutput", value);
      if (state.buildPollController === controller) state.buildPollController = null;
      startBuildPolling(value, { inspect: true, appid: selectedAppid });
      updateHistoryDetailStatus(operation);
      return value;
    } catch (error) {
      if (!buildContextMatches(generation, buildId, appContext, appInput, { inspect: true }) || !historyDetailContextMatches(operation) || error?.name === "AbortError") return null;
      operation.buildPending = false;
      operation.buildError = errorMessage(error);
      renderDetailResult("historyDetailBuild", { status: "rejected", reason: error });
      markBuildStale();
      setBuildPollStatus(`状态未刷新（${errorMessage(error)}）；约 5 秒后重试。`, "error");
      if (error instanceof ApiError && [401, 403].includes(error.status)) {
        stopBuildPolling("认证或权限验证失败，状态未刷新；自动查询已停止，请重新连接。", "error");
      } else {
        state.buildPollFailures = 1;
        scheduleBuildPoll(generation, buildId, appContext, appInput, BUILD_POLL_INTERVAL_MS, selectedAppid);
      }
      updateHistoryDetailStatus(operation);
      return null;
    } finally {
      if (state.buildPollController === controller) state.buildPollController = null;
    }
  }

  function stopHistoryPolling() {
    if (state.historyTimer) {
      clearInterval(state.historyTimer);
      state.historyTimer = null;
    }
  }

  function resetBuildResources() {
    state.resourceBuildGeneration += 1;
    if (state.resourceBuildTimer) clearTimeout(state.resourceBuildTimer);
    if (state.resourceBuildController) state.resourceBuildController.abort();
    state.resourceBuildTimer = null;
    state.resourceBuildController = null;
    state.resourceBuildReport = null;
    state.resourceBuildBusy = false;
    $("cleanupBuildResourcesButton").disabled = true;
    $("buildResourcesOutput").textContent = "{}";
    $("buildResourcesStatus").textContent = "请查询当前 Build 的资源；清理会停止服务并删除私有数据。";
  }

  async function loadBuildResources(cleanup = false) {
    const buildId = $("buildResourcesId").value.trim();
    if (!/^[a-f0-9]{32}$/.test(buildId)) throw new Error("请输入有效的 32 位 Build ID");
    if (state.resourceBuildBusy) return;
    if (cleanup) {
      const report = state.resourceBuildReport;
      if (!report || report.build_id !== buildId || !report.cleanup_allowed) throw new Error("请先查询可清理的构建资源");
      if (!window.confirm(`确认清理构建 ${buildId}？这将停止该构建服务，并永久删除其私有数据、挂载存储及镜像引用。`)) return;
    }
    if (state.resourceBuildTimer) clearTimeout(state.resourceBuildTimer);
    const generation = state.resourceBuildGeneration;
    const controller = new AbortController();
    state.resourceBuildController = controller;
    state.resourceBuildBusy = true;
    $("cleanupBuildResourcesButton").disabled = true;
    $("buildResourcesStatus").textContent = cleanup ? "正在提交构建清理..." : "正在查询构建资源...";
    try {
      const report = await request(`/api/v1/jenkins/build_resources/${encodeURIComponent(buildId)}`, {
        method: cleanup ? "DELETE" : "GET", signal: controller.signal,
      });
      if (generation !== state.resourceBuildGeneration || $("buildResourcesId").value.trim() !== buildId) return;
      if (report.build_id !== buildId) throw new Error("接口返回的构建资源与查询 Build ID 不一致");
      state.resourceBuildReport = report;
      $("buildResourcesOutput").textContent = JSON.stringify(report, null, 2);
      $("buildResourcesStatus").textContent = `清理状态：${report.cleanup_status}；镜像存储：${report.registry_storage_status}；刷新：${formatTime()}${report.last_error ? `；${report.last_error}` : ""}${report.cleanup_reason ? `；${report.cleanup_reason}` : ""}`;
      $("cleanupBuildResourcesButton").disabled = !report.cleanup_allowed;
      if (["queued", "running"].includes(report.cleanup_status)) {
        state.resourceBuildTimer = setTimeout(() => {
          loadBuildResources().catch((error) => {
            if (generation === state.resourceBuildGeneration) $("buildResourcesStatus").textContent = `清理状态查询失败，请手动刷新：${error.message}`;
          });
        }, BUILD_POLL_INTERVAL_MS);
      }
    } catch (error) {
      if (generation !== state.resourceBuildGeneration || error.name === "AbortError") return;
      $("buildResourcesStatus").textContent = `操作失败：${error.message}。请重新查询；清理请求可能已经受理。`;
      throw error;
    } finally {
      if (generation === state.resourceBuildGeneration) {
        state.resourceBuildBusy = false;
        state.resourceBuildController = null;
      }
    }
  }

  function updateHistoryPolling() {
    stopHistoryPolling();
    if (!state.token || !$("historyAutoRefresh").checked) return;
    state.historyTimer = setInterval(() => {
      if (state.historyLoading) return;
      loadHistory({ silent: true }).catch((error) => {
        if (state.token) setActivity(notifyError(error), "error");
      });
    }, 10000);
  }

  async function loadResource(resource) {
    const appid = appidFromInput();
    const version = state.appContextVersion;
    const labels = { events: "事件", images: "用户镜像", services: "Docker Services", access: "访问入口", alerts: "告警" };
    const resourcePath = {
      events: "app_events",
      images: "app_images",
      services: "app_services",
      access: "app_access",
      alerts: "app_alerts",
    }[resource];
    const value = await request(`/api/v1/jenkins/${resourcePath}/${encodeURIComponent(appid)}`);
    if (!sameAppContext(appid, version)) return null;
    $("resourceTitle").textContent = labels[resource] || resource;
    $("resourceTimestamp").textContent = formatTime();
    pretty("resourceOutput", value);
    if (resource === "services" && Array.isArray(value)) $("metricServices").textContent = String(value.length);
    if (resource === "access") renderServiceAccess(value);
    return value;
  }

  function templateElement(...ids) {
    for (const id of ids) {
      const element = $(id);
      if (element) return element;
    }
    return null;
  }

  function setTechStackStatus(message) {
    const target = $("techStackStatus");
    if (target) target.textContent = message;
  }

  function techStackEditorFields() {
    return {
      id: $("techStackId"),
      name: $("techStackName"),
      yaml: $("techStackYaml"),
      json: $("techStackJson"),
      comments: $("techStackComments"),
    };
  }

  function setTechStackEditorMode(isExisting) {
    const fields = techStackEditorFields();
    if (fields.id) fields.id.readOnly = isExisting;
    const createButton = $("createTechStackButton");
    const saveButton = $("saveTechStackButton");
    const deleteButton = $("deleteTechStackButton");
    if (createButton) createButton.disabled = isExisting;
    if (saveButton) saveButton.disabled = !isExisting;
    if (deleteButton) deleteButton.disabled = !isExisting;
  }

  function applyTechStack(stack) {
    if (!stack) return;
    state.selectedTechStackId = stack.tech_stack_id || "";
    const fields = techStackEditorFields();
    if (fields.id) fields.id.value = stack.tech_stack_id || "";
    if (fields.name) fields.name.value = stack.name || "";
    if (fields.yaml) fields.yaml.value = stack.yaml_original || "";
    if (fields.json) fields.json.value = JSON.stringify(stack.json_data || {}, null, 2);
    if (fields.comments) fields.comments.value = JSON.stringify(stack.line_comments || {}, null, 2);
    setTechStackEditorMode(true);
    renderTechStackList();
    setTechStackStatus(`正在编辑 ${stack.name || stack.tech_stack_id}`);
  }

  function newTechStack() {
    state.selectedTechStackId = "";
    const fields = techStackEditorFields();
    if (fields.id) fields.id.value = "";
    if (fields.name) fields.name.value = "";
    if (fields.yaml) fields.yaml.value = "";
    if (fields.json) fields.json.value = "{}";
    if (fields.comments) fields.comments.value = "{}";
    setTechStackEditorMode(false);
    renderTechStackList();
    setTechStackStatus("正在新建技术栈，请填写全部必填字段。");
    fields.id?.focus();
  }

  function resetTechStack() {
    const selected = state.techStacks.find((stack) => stack.tech_stack_id === state.selectedTechStackId);
    if (selected) applyTechStack(selected);
    else newTechStack();
  }

  function renderTechStackList() {
    const root = $("techStackList");
    const count = $("techStackCount");
    if (!root && !count) return;
    const query = ($("techStackSearch")?.value || "").trim().toLowerCase();
    const filtered = state.techStacks.filter((stack) => {
      return !query || [stack.tech_stack_id, stack.name].some((value) => String(value || "").toLowerCase().includes(query));
    });
    if (count) {
      count.textContent = query
        ? `${filtered.length} / ${state.techStacks.length} 个技术栈`
        : `${state.techStacks.length} 个技术栈`;
    }
    if (!root) return;
    root.replaceChildren();
    filtered.forEach((stack) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "tech-stack-item";
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", String(stack.tech_stack_id === state.selectedTechStackId));
      const id = document.createElement("span");
      id.className = "tech-stack-item-id";
      id.textContent = stack.tech_stack_id;
      const name = document.createElement("span");
      name.className = "tech-stack-item-name";
      name.textContent = stack.name || stack.tech_stack_id;
      button.append(id, name);
      button.addEventListener("click", () => withFeedback(() => selectTechStack(stack.tech_stack_id), null));
      root.appendChild(button);
    });
    if (!filtered.length) {
      const empty = document.createElement("p");
      empty.className = "muted small";
      empty.textContent = query ? "没有匹配的技术栈。" : "暂无技术栈。";
      root.appendChild(empty);
    }
  }

  function componentRoots() {
    const ids = ["templateComponents", "composeComponents", "composeStackPicker"];
    return ids.map((id) => $(id)).filter((root, index, roots) => root && roots.indexOf(root) === index);
  }

  function selectedTemplateComponents() {
    const selected = new Set();
    componentRoots().forEach((root) => {
      if (root instanceof HTMLSelectElement) {
        [...root.selectedOptions].forEach((option) => selected.add(option.value));
      } else {
        root.querySelectorAll("input:checked").forEach((input) => selected.add(input.value));
      }
    });
    return [...selected];
  }

  function renderTemplateComponents(components) {
    const previouslySelected = new Set(selectedTemplateComponents());
    componentRoots().forEach((root) => {
      root.replaceChildren();
      if (root instanceof HTMLSelectElement) {
        components.forEach((component) => {
          const option = document.createElement("option");
          option.value = component;
          option.textContent = component;
          option.selected = previouslySelected.has(component);
          root.appendChild(option);
        });
        return;
      }
      components.forEach((component) => {
        const label = document.createElement("label");
        label.className = "component-choice";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = component;
        input.checked = previouslySelected.has(component);
        const name = document.createElement("span");
        name.textContent = component;
        label.append(input, name);
        root.appendChild(label);
      });
      if (!components.length) {
        const empty = document.createElement("span");
        empty.className = "muted small";
        empty.textContent = "暂无组件";
        root.appendChild(empty);
      }
    });
  }

  async function loadTemplates() {
    const [templates, techStacks] = await Promise.all([
      request("/api/v1/docker/template_list"),
      request("/api/v1/docker/tech_stack_list"),
    ]);
    const components = Array.isArray(templates?.components) ? templates.components : [];
    state.techStacks = Array.isArray(techStacks)
      ? techStacks
      : Array.isArray(templates?.tech_stacks) ? templates.tech_stacks : [];
    if (state.selectedTechStackId && !state.techStacks.some((stack) => stack.tech_stack_id === state.selectedTechStackId)) {
      state.selectedTechStackId = "";
    }
    renderTemplateComponents(components);
    renderTechStackList();
    setTechStackEditorMode(Boolean(state.selectedTechStackId));
    setTechStackStatus(`已加载 ${state.techStacks.length} 个技术栈、${components.length} 个可组合组件。`);
    setActivity(`已加载 ${components.length} 个模板组件`, "success");
    return { ...templates, tech_stacks: state.techStacks };
  }

  async function selectTechStack(techStackId) {
    const value = await request(`/api/v1/docker/tech_stack_info/${encodeURIComponent(techStackId)}`);
    const index = state.techStacks.findIndex((stack) => stack.tech_stack_id === techStackId);
    if (index >= 0) state.techStacks[index] = value;
    else state.techStacks.push(value);
    applyTechStack(value);
    return value;
  }

  function techStackPayload() {
    const fields = techStackEditorFields();
    const techStackId = fields.id?.value.trim() || "";
    const name = fields.name?.value.trim() || "";
    const yamlOriginal = fields.yaml?.value || "";
    if (!techStackId || !name || !yamlOriginal.trim()) throw new Error("技术栈 ID、名称和 YAML 原文均为必填项");
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(techStackId)) {
      throw new Error("技术栈 ID 只能包含字母、数字、下划线和连字符，且必须以字母或数字开头");
    }
    const jsonData = parsedJson(fields.json?.value || "", "JSON 数据", false);
    const lineComments = parsedJson(fields.comments?.value || "{}", "路径注释", false);
    if (!jsonData || typeof jsonData !== "object" || Array.isArray(jsonData)) throw new Error("JSON 数据必须是对象");
    if (!lineComments || typeof lineComments !== "object" || Array.isArray(lineComments)) throw new Error("路径注释必须是 JSON 对象");
    return {
      tech_stack_id: techStackId,
      name,
      yaml_original: yamlOriginal,
      json_data: jsonData,
      line_comments: lineComments,
    };
  }

  async function saveTechStack(forceCreate = false) {
    const payload = techStackPayload();
    const existingId = state.selectedTechStackId;
    if (existingId && payload.tech_stack_id !== existingId) throw new Error("已存在技术栈的 ID 不可修改");
    let value;
    if (!forceCreate && existingId) {
      const body = { ...payload };
      delete body.tech_stack_id;
      value = await request(`/api/v1/docker/tech_stack_info/${encodeURIComponent(existingId)}`, { method: "PATCH", body });
    } else {
      value = await request("/api/v1/docker/tech_stack_create", { method: "POST", body: payload });
    }
    state.selectedTechStackId = value.tech_stack_id;
    await loadTemplates();
    applyTechStack(value);
    setTechStackStatus(`${forceCreate || !existingId ? "已创建" : "已保存"}技术栈 ${value.tech_stack_id}。`);
    return value;
  }

  async function deleteTechStack() {
    const techStackId = state.selectedTechStackId;
    if (!techStackId) throw new Error("请先选择要删除的技术栈");
    if (!window.confirm(`确定删除技术栈 ${techStackId}？此操作无法撤销。`)) return null;
    await request(`/api/v1/docker/tech_stack_info/${encodeURIComponent(techStackId)}`, { method: "DELETE" });
    state.selectedTechStackId = "";
    newTechStack();
    await loadTemplates();
    setTechStackStatus(`已删除技术栈 ${techStackId}。`);
    return { deleted: techStackId };
  }

  function setAllTemplateComponents(checked) {
    componentRoots().forEach((root) => {
      if (root instanceof HTMLSelectElement) {
        [...root.options].forEach((option) => { option.selected = checked; });
      } else {
        root.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.checked = checked; });
      }
    });
  }

  async function composeTemplate() {
    ensureAppIdle();
    const appid = state.appid;
    const version = state.appContextVersion;
    const components = selectedTemplateComponents();
    if (!components.length) throw new Error("请至少选择一个模板组件");
    const dependencies = parsedJson($("templateDependencies")?.value || "{}", "依赖关系", false);
    if (!dependencies || typeof dependencies !== "object" || Array.isArray(dependencies)) {
      throw new Error("依赖关系必须是 JSON 对象");
    }
    const format = $("templateFormat")?.value || "json";
    const value = await request(`/api/v1/docker/template_compose?format=${encodeURIComponent(format)}`, {
      method: "POST",
      body: { components, dependencies },
    });
    if ($("templateOutput")) pretty("templateOutput", value);
    if (format === "json" && $("compose") && !state.appOperation && sameAppContext(appid, version)) {
      const document = value?.json_data || value;
      $("compose").value = JSON.stringify(document, null, 2);
    }
    return value;
  }

  async function loadComposePrompt() {
    const value = await request("/api/v1/docker/compose_prompt");
    const output = $("composePromptOutput");
    if (output) output.textContent = typeof value === "string" ? value : "";
    const panel = templateElement("templatePromptPanel", "composePromptPanel");
    if (panel) panel.hidden = false;
    return value;
  }

  async function copyText(text, label) {
    if (!text) throw new Error(`${label}为空，暂无可复制内容`);
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        setActivity(`${label}已复制`, "success");
        return text;
      } catch {
        // Fall back for insecure origins and denied clipboard permissions.
      }
    }
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) throw new Error("浏览器不允许访问剪贴板，请手动选择并复制");
    setActivity(`${label}已复制`, "success");
    return text;
  }

  async function loadBaseImages() {
    const value = await request("/api/v1/docker/base_image_list");
    pretty("baseImagesOutput", value);
    return value;
  }

  async function syncBaseImages() {
    const value = await request("/api/v1/docker/base_image_sync", { method: "POST" });
    pretty("baseImagesOutput", value);
    return value;
  }

  function bindOptional(id, eventName, listener) {
    const element = $(id);
    if (element) element.addEventListener(eventName, listener);
    return element;
  }

  function bindEvents() {
    $("buildResourcesForm").addEventListener("submit", (event) => {
      event.preventDefault();
      withFeedback(() => loadBuildResources(), null);
    });
    $("buildResourcesId").addEventListener("input", resetBuildResources);
    $("cleanupBuildResourcesButton").addEventListener("click", () => withFeedback(() => loadBuildResources(true), null));
    $("connectForm").addEventListener("submit", (event) => {
      event.preventDefault();
      withFeedback(connect, null);
    });
    $("disconnectButton").addEventListener("click", () => {
      if (state.appOperation) return;
      state.token = "";
      state.refreshToken = "";
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(REFRESH_TOKEN_KEY);
      stopHistoryPolling();
      stopBuildCreation();
      stopHistoryDetailLoading();
      resetWorkspaceView("已退出登录，请重新连接后查看文件。");
      resetBuildResources();
      markBuildStale();
      stopBuildPolling("已退出登录，自动查询已停止。", "muted");
      setSession(false, "已清除当前浏览器中的访问令牌。");
      setActivity("已退出", "success");
    });
    $("healthButton").addEventListener("click", () => withFeedback(async () => {
      const value = await request("/healthz");
      $("metricHealth").textContent = value.status || "ok";
      return value;
    }, null));
    ["openAppContextModal", "openConnectionModal", "workspaceSelectAppButton"].forEach((id) => {
      $(id).addEventListener("click", (event) => {
        if (state.appOperation) event.preventDefault();
      });
    });
    ["openAppContextModal", "workspaceSelectAppButton"].forEach((id) => {
      $(id).addEventListener("click", () => {
        if (state.appOperation) return;
        if (state.token) withFeedback(() => loadAppChoices(), null);
      });
    });
    $("refreshContextAppsButton").addEventListener("click", () => withFeedback(loadAppChoices, null));
    $("loadContextButton").addEventListener("click", () => {
      withFeedback(selectContextApp, "appOutput").then((value) => {
        if (value) closeModal("appContextModal");
      });
    });
    $("appId").addEventListener("input", (event) => {
      const previous = state.appid;
      const selector = $("contextAppSelect");
      if (selector) selector.value = "";
      state.appid = event.target.value.trim();
      if (state.appid) localStorage.setItem(APP_KEY, state.appid);
      else localStorage.removeItem(APP_KEY);
      if (state.appid !== previous) {
        setCurrentApp("");
        resetAppView();
      }
    });
    $("buildId").addEventListener("input", (event) => {
      if (event.target.value.trim() === state.buildId) return;
      stopBuildPolling("Build ID 已更改；查询旧构建已停止，请查询新 ID。", "muted");
      resetBuildView({ keepInput: true });
    });
    $("environment").addEventListener("input", () => { state.environmentDirty = true; });
    $("appForm").addEventListener("submit", (event) => { event.preventDefault(); withFeedback(createApp, "appOutput"); });
    $("workspaceUploadForm").addEventListener("submit", (event) => {
      event.preventDefault();
      withFeedback(uploadWorkspace, null);
    });
    $("workspaceRefreshButton").addEventListener("click", () => withFeedback(loadWorkspace, null));
    $("workspaceBuildButton").addEventListener("click", () => { window.location.hash = "buildSection"; });
    ["workspaceFiles", "workspaceDirectory"].forEach((id) => {
      $(id).addEventListener("change", () => {
        if ($(id).files.length) $(id === "workspaceFiles" ? "workspaceDirectory" : "workspaceFiles").value = "";
        renderWorkspace();
      });
    });
    $("workspaceSearch").addEventListener("input", () => { state.workspacePage = 1; renderWorkspace(); });
    $("workspacePageSize").addEventListener("change", () => {
      state.workspacePageSize = Number($("workspacePageSize").value);
      state.workspacePage = 1;
      renderWorkspace();
    });
    $("workspacePreviousButton").addEventListener("click", () => { state.workspacePage -= 1; renderWorkspace(); });
    $("workspaceNextButton").addEventListener("click", () => { state.workspacePage += 1; renderWorkspace(); });
    $("getAppButton").addEventListener("click", () => withFeedback(getApp, "appOutput"));
    $("patchAppButton").addEventListener("click", () => withFeedback(patchApp, "appOutput"));
    $("syncAppButton").addEventListener("click", () => {
      if (state.appOperation) return;
      syncApp().catch((error) => {
        const message = notifyError(error);
        setActivity(message, "error");
      });
    });
    $("buildForm").addEventListener("submit", (event) => { event.preventDefault(); withFeedback(createBuild, "buildOutput"); });
    $("buildQueryForm").addEventListener("submit", (event) => { event.preventDefault(); withFeedback(getBuild, "buildOutput"); });
    $("pollBuildButton").addEventListener("click", () => withFeedback(getBuild, "buildOutput"));
    $("historyFilterForm").addEventListener("submit", (event) => {
      event.preventDefault();
      withFeedback(() => loadHistory({ resetPage: true }), null);
    });
    $("historyRefreshButton").addEventListener("click", () => withFeedback(() => loadHistory(), null));
    $("historyPageSize").addEventListener("change", () => withFeedback(() => loadHistory({ resetPage: true }), null));
    $("historyPreviousButton").addEventListener("click", () => {
      if (state.historyPage <= 1) return;
      state.historyPage -= 1;
      withFeedback(() => loadHistory(), null);
    });
    $("historyNextButton").addEventListener("click", () => {
      if (state.historyPage >= historyPageCount()) return;
      state.historyPage += 1;
      withFeedback(() => loadHistory(), null);
    });
    $("historyAutoRefresh").addEventListener("change", updateHistoryPolling);
    $("historyDetailCloseButton").addEventListener("click", () => {
      $("historyDetailPanel").hidden = true;
      stopHistoryDetailLoading();
    });
    document.querySelectorAll(".resource-button").forEach((button) => {
      button.addEventListener("click", () => withFeedback(() => loadResource(button.dataset.resource), "resourceOutput"));
    });
    $("refreshAccessButton").addEventListener("click", () => withFeedback(() => loadResource("access"), "resourceOutput"));
    bindOptional("loadTemplatesButton", "click", () => withFeedback(loadTemplates, null));
    bindOptional("loadComposePromptButton", "click", () => withFeedback(loadComposePrompt, null));
    bindOptional("templateForm", "submit", (event) => {
      event.preventDefault();
      withFeedback(composeTemplate, "templateOutput");
    });
    bindOptional("techStackSearch", "input", renderTechStackList);
    bindOptional("newTechStackButton", "click", newTechStack);
    bindOptional("createTechStackButton", "click", () => withFeedback(() => saveTechStack(true), null));
    bindOptional("saveTechStackButton", "click", () => withFeedback(() => saveTechStack(false), null));
    bindOptional("deleteTechStackButton", "click", () => withFeedback(deleteTechStack, null));
    const techStackForm = templateElement("techStackForm", "techStackEditor");
    if (techStackForm) {
      techStackForm.addEventListener("submit", (event) => {
        event.preventDefault();
        withFeedback(() => saveTechStack(false), null);
      });
      techStackForm.addEventListener("reset", (event) => {
        event.preventDefault();
        resetTechStack();
      });
    } else {
      bindOptional("resetTechStackButton", "click", (event) => {
        event.preventDefault();
        resetTechStack();
      });
    }
    bindOptional("selectAllTechStacksButton", "click", () => setAllTemplateComponents(true));
    bindOptional("clearTechStacksButton", "click", () => setAllTemplateComponents(false));
    ["copyTemplateOutputButton", "copyComposeOutputButton"].forEach((id) => {
      bindOptional(id, "click", () => withFeedback(
        () => copyText($("templateOutput")?.textContent || "", "Compose 结果"),
        null,
      ));
    });
    bindOptional("copyComposePromptButton", "click", () => withFeedback(
      () => copyText($("composePromptOutput")?.textContent || "", "Markdown 提示词"),
      null,
    ));
    $("loadBaseImagesButton").addEventListener("click", () => withFeedback(loadBaseImages, "baseImagesOutput"));
    $("syncBaseImagesButton").addEventListener("click", () => withFeedback(syncBaseImages, "baseImagesOutput"));
  }

  async function restoreSession() {
    if (state.appid) {
      $("appId").value = state.appid;
    }
    setCurrentApp(state.appid ? state.appName : "");
    if (!state.token) return;
    try {
      setSession(true, "访问令牌已恢复");
      await Promise.all([loadTemplates(), loadBaseImages(), loadHistory({ silent: true }), loadAppChoices()]);
      updateHistoryPolling();
    } catch {
      setSession(false);
      stopHistoryPolling();
    }
  }

  bindEvents();
  bindModals();
  bindNavigation();
  rememberSection();
  setSession(Boolean(state.token));
  setCurrentApp(state.appid ? state.appName : "");
  window.addEventListener("pagehide", () => {
    resetBuildResources();
    stopHistoryPolling();
    stopBuildCreation();
    stopHistoryDetailLoading();
    resetWorkspaceView("页面已关闭，文件操作结果请重新打开后刷新确认。");
    stopBuildPolling("页面已关闭，自动查询已停止。", "muted");
  });
  restoreSession();
})();
