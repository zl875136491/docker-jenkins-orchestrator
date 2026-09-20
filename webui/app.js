(() => {
  "use strict";

  const TOKEN_KEY = "orchestrator.control.token";
  const APP_KEY = "orchestrator.control.appid";
  const state = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    appid: localStorage.getItem(APP_KEY) || "",
    buildId: "",
    latestBuild: null,
    historyPage: 1,
    historyPageSize: 20,
    historyTotal: 0,
    historyLoading: false,
    historyTimer: null,
  };

  const $ = (id) => document.getElementById(id);
  const apiOrigin = window.location.origin;
  $("apiOrigin").textContent = apiOrigin;

  class ApiError extends Error {
    constructor(status, payload) {
      const detail = payload && payload.detail;
      const message = Array.isArray(detail)
        ? detail.map((item) => item.msg || JSON.stringify(item)).join("; ")
        : detail || payload?.message || `请求失败（HTTP ${status}）`;
      super(message);
      this.status = status;
      this.payload = payload;
    }
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
    $("sessionDetail").textContent = detail || (online ? "JWT 已保存到当前浏览器。" : "请输入 worker 凭据获取 JWT。");
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
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function parsedJson(text, label, allowEmpty = false) {
    if (!text.trim() && allowEmpty) return undefined;
    try {
      return JSON.parse(text);
    } catch (error) {
      throw new Error(`${label} 不是有效 JSON`);
    }
  }

  function appidFromInput() {
    const value = $("appId").value.trim() || $("contextAppId").value.trim() || state.appid;
    if (!value) throw new Error("请先填写 App ID");
    state.appid = value;
    localStorage.setItem(APP_KEY, value);
    $("appId").value = value;
    $("contextAppId").value = value;
    $("metricApp").textContent = value;
    return value;
  }

  function applyApp(app) {
    state.appid = app.appid || "";
    localStorage.setItem(APP_KEY, state.appid);
    $("appId").value = app.appid || "";
    $("contextAppId").value = app.appid || "";
    $("appName").value = app.name || "";
    $("repositoryUrl").value = app.repository_url || "";
    $("gitRef").value = app.git_ref || "main";
    $("components").value = (app.components || []).join(",");
    $("compose").value = app.compose ? JSON.stringify(app.compose, null, 2) : "";
    $("metricApp").textContent = app.appid || "未选择";
  }

  function applyBuild(build) {
    state.buildId = build.build_id || state.buildId;
    state.latestBuild = build;
    $("buildId").value = state.buildId;
    $("metricBuild").textContent = build.status || "--";
    $("buildStatus").textContent = build.status || "--";
    $("buildStatus").className = `status-pill ${statusClass(build.status)}`;
    $("jenkinsBuild").textContent = build.jenkins_build_number ?? "--";
    $("celeryTask").textContent = build.celery_task_id || "--";
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body !== undefined && !(options.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
      options.body = JSON.stringify(options.body);
    }
    if (state.token && !path.endsWith("/connect")) headers.set("Authorization", `Bearer ${state.token}`);
    const response = await fetch(path, { ...options, headers });
    const text = await response.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = text; }
    }
    if (!response.ok) {
      if (response.status === 401 && !path.endsWith("/connect")) {
        state.token = "";
        localStorage.removeItem(TOKEN_KEY);
        stopHistoryPolling();
        setSession(false, "访问令牌已失效，请重新连接。");
      }
      throw new ApiError(response.status, payload);
    }
    return payload;
  }

  async function withFeedback(action, outputId) {
    try {
      const value = await action();
      if (outputId) pretty(outputId, value);
      setActivity("请求完成", "success");
      return value;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (outputId) pretty(outputId, { error: message });
      setActivity(message, "error");
      return null;
    }
  }

  async function connect() {
    const payload = await request("/api/connect", {
      method: "POST",
      body: { worker_name: $("workerName").value.trim(), worker_secret: $("workerSecret").value },
    });
    state.token = payload.access_token;
    localStorage.setItem(TOKEN_KEY, state.token);
    setSession(true, `令牌有效至 ${payload.expires_at || "未知时间"}`);
    await Promise.all([loadTemplates(), loadBaseImages(), loadHistory({ resetPage: true, silent: true })]);
    updateHistoryPolling();
    return payload;
  }

  function appPayload() {
    const appid = appidFromInput();
    const components = $("components").value.split(",").map((item) => item.trim()).filter(Boolean);
    return {
      appid,
      name: $("appName").value.trim(),
      repository_url: $("repositoryUrl").value.trim(),
      git_ref: $("gitRef").value.trim() || "main",
      environment: parsedJson($("environment").value, "环境变量", false),
      compose: parsedJson($("compose").value, "Compose", true) ?? null,
      components,
    };
  }

  async function createApp() {
    const value = await request("/api/apps", { method: "POST", body: appPayload() });
    applyApp(value);
    pretty("appOutput", value);
    return value;
  }

  async function getApp() {
    const appid = appidFromInput();
    const value = await request(`/api/apps/${encodeURIComponent(appid)}`);
    applyApp(value);
    pretty("appOutput", value);
    return value;
  }

  async function patchApp() {
    const payload = appPayload();
    const appid = payload.appid;
    delete payload.appid;
    const value = await request(`/api/apps/${encodeURIComponent(appid)}`, { method: "PATCH", body: payload });
    applyApp(value);
    pretty("appOutput", value);
    return value;
  }

  async function createBuild() {
    const appid = appidFromInput();
    const gitRef = $("buildGitRef").value.trim();
    const body = gitRef ? { git_ref: gitRef } : {};
    const value = await request(`/api/apps/${encodeURIComponent(appid)}/builds`, { method: "POST", body });
    applyBuild(value);
    pretty("buildOutput", value);
    loadHistory({ resetPage: true, silent: true }).catch(() => {});
    return value;
  }

  async function getBuild() {
    const buildId = $("buildId").value.trim() || state.buildId;
    if (!buildId) throw new Error("请先填写 Build ID");
    state.buildId = buildId;
    const value = await request(`/api/builds/${encodeURIComponent(buildId)}`);
    applyBuild(value);
    pretty("buildOutput", value);
    return value;
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
    if (appid) params.set("appid", appid);
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
      const value = await request(`/api/builds?${params.toString()}`);
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

  function renderDetailResult(targetId, result) {
    const target = $(targetId);
    prettyNode(target, result.status === "fulfilled" ? result.value : detailError(result.reason));
  }

  function resourcesForBuild(value, buildId) {
    if (!Array.isArray(value)) return value;
    return value.filter((item) => !item?.build_id || item.build_id === buildId);
  }

  async function inspectBuild(buildId, appid) {
    if (!buildId) throw new Error("缺少 Build ID");
    const selectedAppid = appid || state.appid || $("historyAppId").value.trim();
    if (!selectedAppid) throw new Error("该任务没有可用的 App ID");
    $("historyDetailPanel").hidden = false;
    $("historyDetailTitle").textContent = `任务详情：${buildId}`;
    $("historyDetailMeta").textContent = `App ID：${selectedAppid}`;
    $("historyDetailStatus").textContent = "正在并行加载任务及关联资源...";
    ["historyDetailBuild", "historyDetailEvents", "historyDetailImages", "historyDetailServices", "historyDetailAlerts"].forEach((id) => {
      $(id).textContent = "加载中...";
    });

    const results = await Promise.allSettled([
      request(`/api/builds/${encodeURIComponent(buildId)}`),
      request(`/api/apps/${encodeURIComponent(selectedAppid)}/events`),
      request(`/api/apps/${encodeURIComponent(selectedAppid)}/images`),
      request(`/api/apps/${encodeURIComponent(selectedAppid)}/services`),
      request(`/api/apps/${encodeURIComponent(selectedAppid)}/alerts`),
    ]);
    const [buildResult, eventsResult, imagesResult, servicesResult, alertsResult] = results;
    renderDetailResult("historyDetailBuild", buildResult);
    renderDetailResult("historyDetailEvents", eventsResult.status === "fulfilled"
      ? { ...eventsResult, value: resourcesForBuild(eventsResult.value, buildId) }
      : eventsResult);
    renderDetailResult("historyDetailImages", imagesResult.status === "fulfilled"
      ? { ...imagesResult, value: resourcesForBuild(imagesResult.value, buildId) }
      : imagesResult);
    renderDetailResult("historyDetailServices", servicesResult.status === "fulfilled"
      ? { ...servicesResult, value: resourcesForBuild(servicesResult.value, buildId) }
      : servicesResult);
    renderDetailResult("historyDetailAlerts", alertsResult.status === "fulfilled"
      ? { ...alertsResult, value: resourcesForBuild(alertsResult.value, buildId) }
      : alertsResult);
    if (buildResult.status === "fulfilled") applyBuild(buildResult.value);
    const failed = results.filter((result) => result.status === "rejected").length;
    $("historyDetailStatus").textContent = failed
      ? `详情已加载，${failed} 个关联接口返回失败。`
      : `详情已更新：${new Date().toLocaleTimeString()}`;
    return buildResult.status === "fulfilled" ? buildResult.value : null;
  }

  function stopHistoryPolling() {
    if (state.historyTimer) {
      clearInterval(state.historyTimer);
      state.historyTimer = null;
    }
  }

  function updateHistoryPolling() {
    stopHistoryPolling();
    if (!state.token || !$("historyAutoRefresh").checked) return;
    state.historyTimer = setInterval(() => {
      if (state.historyLoading) return;
      loadHistory({ silent: true }).catch((error) => {
        if (state.token) setActivity(error instanceof Error ? error.message : String(error), "error");
      });
    }, 10000);
  }

  async function loadResource(resource) {
    const appid = appidFromInput();
    const labels = { events: "事件", images: "用户镜像", services: "Docker Services", alerts: "告警" };
    const value = await request(`/api/apps/${encodeURIComponent(appid)}/${resource}`);
    $("resourceTitle").textContent = labels[resource] || resource;
    $("resourceTimestamp").textContent = new Date().toLocaleTimeString();
    pretty("resourceOutput", value);
    if (resource === "services" && Array.isArray(value)) $("metricServices").textContent = String(value.length);
    return value;
  }

  async function loadTemplates() {
    const value = await request("/api/templates");
    const root = $("templateComponents");
    root.textContent = "";
    (value.components || []).forEach((component) => {
      const label = document.createElement("label");
      label.className = "component-choice";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = component;
      const name = document.createElement("span");
      name.textContent = component;
      label.append(input, name);
      root.appendChild(label);
    });
    if (!value.components?.length) {
      const empty = document.createElement("span");
      empty.className = "muted small";
      empty.textContent = "暂无组件";
      root.appendChild(empty);
    }
    setActivity(`已加载 ${value.components?.length || 0} 个模板组件`, "success");
    return value;
  }

  async function composeTemplate() {
    const components = [...document.querySelectorAll("#templateComponents input:checked")].map((input) => input.value);
    const dependencies = parsedJson($("templateDependencies").value, "依赖关系", false);
    const value = await request("/api/templates/compose", { method: "POST", body: { components, dependencies } });
    pretty("templateOutput", value);
    $("compose").value = JSON.stringify(value, null, 2);
    return value;
  }

  async function loadBaseImages() {
    const value = await request("/api/base-images");
    pretty("baseImagesOutput", value);
    return value;
  }

  async function syncBaseImages() {
    const value = await request("/api/base-images/sync", { method: "POST" });
    pretty("baseImagesOutput", value);
    return value;
  }

  function bindEvents() {
    $("connectForm").addEventListener("submit", (event) => {
      event.preventDefault();
      withFeedback(connect, null);
    });
    $("disconnectButton").addEventListener("click", () => {
      state.token = "";
      localStorage.removeItem(TOKEN_KEY);
      stopHistoryPolling();
      setSession(false, "已清除当前浏览器中的访问令牌。");
      setActivity("已退出", "success");
    });
    $("healthButton").addEventListener("click", () => withFeedback(async () => {
      const value = await request("/healthz");
      $("metricHealth").textContent = value.status || "ok";
      return value;
    }, null));
    $("loadContextButton").addEventListener("click", () => withFeedback(getApp, "appOutput"));
    $("appId").addEventListener("input", (event) => {
      $("contextAppId").value = event.target.value;
      state.appid = event.target.value.trim();
      if (state.appid) localStorage.setItem(APP_KEY, state.appid);
      $("metricApp").textContent = state.appid || "未选择";
    });
    $("contextAppId").addEventListener("input", (event) => { $("appId").value = event.target.value; });
    $("appForm").addEventListener("submit", (event) => { event.preventDefault(); withFeedback(createApp, "appOutput"); });
    $("getAppButton").addEventListener("click", () => withFeedback(getApp, "appOutput"));
    $("patchAppButton").addEventListener("click", () => withFeedback(patchApp, "appOutput"));
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
    });
    document.querySelectorAll(".resource-button").forEach((button) => {
      button.addEventListener("click", () => withFeedback(() => loadResource(button.dataset.resource), "resourceOutput"));
    });
    $("loadTemplatesButton").addEventListener("click", () => withFeedback(loadTemplates, null));
    $("templateForm").addEventListener("submit", (event) => { event.preventDefault(); withFeedback(composeTemplate, "templateOutput"); });
    $("loadBaseImagesButton").addEventListener("click", () => withFeedback(loadBaseImages, "baseImagesOutput"));
    $("syncBaseImagesButton").addEventListener("click", () => withFeedback(syncBaseImages, "baseImagesOutput"));
  }

  async function restoreSession() {
    if (state.appid) {
      $("appId").value = state.appid;
      $("contextAppId").value = state.appid;
      $("metricApp").textContent = state.appid;
    }
    if (!state.token) return;
    try {
      const me = await request("/api/me");
      setSession(true, `当前身份：${me.worker_name || "conductor"}`);
      await Promise.all([loadTemplates(), loadBaseImages(), loadHistory({ silent: true })]);
      updateHistoryPolling();
    } catch {
      setSession(false);
      stopHistoryPolling();
    }
  }

  bindEvents();
  setSession(Boolean(state.token));
  restoreSession();
})();
