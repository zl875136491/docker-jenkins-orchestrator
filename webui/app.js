(() => {
  "use strict";

  const TOKEN_KEY = "orchestrator.control.token";
  const APP_KEY = "orchestrator.control.appid";
  const state = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    appid: localStorage.getItem(APP_KEY) || "",
    buildId: "",
    latestBuild: null,
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
    const status = String(build.status || "").toLowerCase();
    $("buildStatus").className = `status-pill ${status === "succeeded" ? "success" : status === "failed" || status === "cancelled" ? "failed" : status ? "active" : "neutral"}`;
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
    await Promise.all([loadTemplates(), loadBaseImages()]);
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
    if (!value.components?.length) root.innerHTML = '<span class="muted small">暂无组件</span>';
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
      await Promise.all([loadTemplates(), loadBaseImages()]);
    } catch {
      setSession(false);
    }
  }

  bindEvents();
  setSession(Boolean(state.token));
  restoreSession();
})();
