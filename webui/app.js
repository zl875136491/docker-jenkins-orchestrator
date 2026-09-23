(() => {
  "use strict";

  const TOKEN_KEY = "orchestrator.control.token";
  const REFRESH_TOKEN_KEY = "orchestrator.control.refresh-token";
  const APP_KEY = "orchestrator.control.appid";
  const APP_NAME_KEY = "orchestrator.control.appname";
  const TOAST_TIMEOUT_MS = 5000;
  const notifiedErrors = new WeakSet();
  const state = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    refreshToken: localStorage.getItem(REFRESH_TOKEN_KEY) || "",
    appid: localStorage.getItem(APP_KEY) || "",
    appName: localStorage.getItem(APP_NAME_KEY) || "",
    activeSection: "appSection",
    buildId: "",
    latestBuild: null,
    historyPage: 1,
    historyPageSize: 20,
    historyTotal: 0,
    historyLoading: false,
    historyTimer: null,
    techStacks: [],
    selectedTechStackId: "",
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
    const value = $("appId").value.trim() || state.appid;
    if (!value) throw new Error("请先填写 App ID");
    if (value !== state.appid) setCurrentApp("");
    state.appid = value;
    localStorage.setItem(APP_KEY, value);
    $("appId").value = value;
    const selector = $("contextAppSelect");
    if (selector) selector.value = value;
    return value;
  }

  function applyApp(app) {
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
    $("compose").value = app.compose ? JSON.stringify(app.compose, null, 2) : "";
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
    const appid = $("contextAppSelect").value.trim();
    if (!appid) throw new Error("请先选择已有应用");
    const value = await request(`/api/v1/jenkins/app_info/${encodeURIComponent(appid)}`);
    applyApp(value);
    return value;
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

  async function refreshAccessToken() {
    if (!state.refreshToken) throw new Error("没有可用的刷新令牌");
    const response = await fetch("/oauth2/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: state.refreshToken }),
    });
    const text = await response.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = text; }
    }
    if (!response.ok) throw new ApiError(response.status, payload);
    state.token = payload.access_token;
    state.refreshToken = payload.refresh_token || state.refreshToken;
    localStorage.setItem(TOKEN_KEY, state.token);
    localStorage.setItem(REFRESH_TOKEN_KEY, state.refreshToken);
    return payload;
  }

  async function request(path, options = {}, allowRefresh = true) {
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
      if (!response.ok) {
        if (response.status === 401 && allowRefresh && !path.startsWith("/oauth2/") && state.refreshToken) {
          try {
            await refreshAccessToken();
            return request(path, options, false);
          } catch {
            // Fall through and report the original unauthorized response.
          }
        }
        if (response.status === 401 && !path.startsWith("/oauth2/")) {
          state.token = "";
          state.refreshToken = "";
          localStorage.removeItem(TOKEN_KEY);
          localStorage.removeItem(REFRESH_TOKEN_KEY);
          stopHistoryPolling();
          setSession(false, "访问令牌已失效，请重新连接。");
        }
        throw new ApiError(response.status, payload);
      }
      return payload;
    } catch (error) {
      notifyError(error);
      throw error;
    }
  }

  async function withFeedback(action, outputId) {
    try {
      const value = await action();
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
    closeModal("connectionModal");
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
    const value = await request("/api/v1/jenkins/app_create", { method: "POST", body: appPayload() });
    applyApp(value);
    pretty("appOutput", value);
    return value;
  }

  async function getApp() {
    const appid = appidFromInput();
    const value = await request(`/api/v1/jenkins/app_info/${encodeURIComponent(appid)}`);
    applyApp(value);
    pretty("appOutput", value);
    return value;
  }

  async function patchApp() {
    const payload = appPayload();
    const appid = payload.appid;
    delete payload.appid;
    const value = await request(`/api/v1/jenkins/app_info/${encodeURIComponent(appid)}`, { method: "PATCH", body: payload });
    applyApp(value);
    pretty("appOutput", value);
    return value;
  }

  async function createBuild() {
    const appid = appidFromInput();
    const gitRef = $("buildGitRef").value.trim();
    const body = gitRef ? { git_ref: gitRef } : {};
    const value = await request(`/api/v1/jenkins/build_create/${encodeURIComponent(appid)}`, { method: "POST", body });
    applyBuild(value);
    pretty("buildOutput", value);
    loadHistory({ resetPage: true, silent: true }).catch(() => {});
    return value;
  }

  async function getBuild() {
    const buildId = $("buildId").value.trim() || state.buildId;
    if (!buildId) throw new Error("请先填写 Build ID");
    state.buildId = buildId;
    const value = await request(`/api/v1/jenkins/build_info/${encodeURIComponent(buildId)}`);
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
      request(`/api/v1/jenkins/build_info/${encodeURIComponent(buildId)}`),
      request(`/api/v1/jenkins/app_events/${encodeURIComponent(selectedAppid)}`),
      request(`/api/v1/jenkins/app_images/${encodeURIComponent(selectedAppid)}`),
      request(`/api/v1/jenkins/app_services/${encodeURIComponent(selectedAppid)}`),
      request(`/api/v1/jenkins/app_alerts/${encodeURIComponent(selectedAppid)}`),
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
        if (state.token) setActivity(notifyError(error), "error");
      });
    }, 10000);
  }

  async function loadResource(resource) {
    const appid = appidFromInput();
    const labels = { events: "事件", images: "用户镜像", services: "Docker Services", access: "访问入口", alerts: "告警" };
    const resourcePath = {
      events: "app_events",
      images: "app_images",
      services: "app_services",
      access: "app_access",
      alerts: "app_alerts",
    }[resource];
    const value = await request(`/api/v1/jenkins/${resourcePath}/${encodeURIComponent(appid)}`);
    $("resourceTitle").textContent = labels[resource] || resource;
    $("resourceTimestamp").textContent = new Date().toLocaleTimeString();
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
    if (format === "json" && $("compose")) {
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
    $("connectForm").addEventListener("submit", (event) => {
      event.preventDefault();
      withFeedback(connect, null);
    });
    $("disconnectButton").addEventListener("click", () => {
      state.token = "";
      state.refreshToken = "";
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(REFRESH_TOKEN_KEY);
      stopHistoryPolling();
      setSession(false, "已清除当前浏览器中的访问令牌。");
      setActivity("已退出", "success");
    });
    $("healthButton").addEventListener("click", () => withFeedback(async () => {
      const value = await request("/healthz");
      $("metricHealth").textContent = value.status || "ok";
      return value;
    }, null));
    $("openAppContextModal").addEventListener("click", () => {
      if (state.token) withFeedback(() => loadAppChoices(), null);
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
      if (state.appid !== previous) setCurrentApp("");
    });
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
  restoreSession();
})();
