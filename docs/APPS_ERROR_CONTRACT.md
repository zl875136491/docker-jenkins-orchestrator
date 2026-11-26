# APPS 构建失败错误契约

**版本**: 2026-11-26  
**适用对象**: APPS / conductor 等消费 `GET /api/v1/jenkins/build_info/{build_id}` 的客户端  
**相关文档**: [APPS_INTEGRATION_GUIDE.md](APPS_INTEGRATION_GUIDE.md)、[COMPOSE_ERROR_CLASSIFICATION.md](COMPOSE_ERROR_CLASSIFICATION.md)、[api-reference.md](api-reference.md)

---

## 一、目标

构建失败时，APPS 必须能同时拿到：

1. **稳定机器码**：`error_detail.code` / `error_detail.subcode`
2. **可操作中文说明**：`error_detail.remediation`（以及 `cause` / `summary`）
3. **原始失败日志**：顶层 `raw_log_excerpt`（与 `failure_logs.raw_log_excerpt` 同源）

禁止把失败笼统覆盖成「构建失败，请联系平台排查」。只有在日志确实不可读时，才允许 `recognized=false`，且仍应尽量回传 `raw_log_excerpt`。

---

## 二、字段优先级（失败时）

| 优先级 | 字段 | 用途 |
| --- | --- | --- |
| 1 | `raw_log_excerpt` | 详情页主展示；ANSI 已剥离，**不做应用秘密脱敏**（应用归用户所有） |
| 2 | `error_detail.code` + `subcode` | 分类、路由、埋点、自动修复建议 |
| 3 | `error_detail.remediation` | 用户可操作中文修复建议 |
| 4 | `error_detail.cause` / `summary` / `reason` | 列表摘要与结构化展示 |
| 5 | `error` | 向后兼容短摘要（告警仍会脱敏已知秘密） |
| 6 | `failure_logs` | 备用日志袋（`source` + `raw_log_excerpt`） |

```python
def render_failure(build: dict) -> dict:
    detail = build.get("error_detail") or {}
    raw = build.get("raw_log_excerpt") or (build.get("failure_logs") or {}).get("raw_log_excerpt")
    return {
        "code": detail.get("code"),
        "subcode": detail.get("subcode"),
        "remediation": detail.get("remediation") or "",
        "summary": detail.get("summary") or build.get("error") or "构建失败，详情见日志",
        "raw_log": raw,
        "recognized": bool(detail.get("recognized")),
    }
```

---

## 三、`error_detail` 契约

### 通用字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 顶级错误码 |
| `subcode` | string | 稳定子码 |
| `summary` | string | 中文/结构化摘要 |
| `remediation` | string | 可操作中文修复建议 |
| `recognized` | bool | 是否命中已知分类 |
| `failure_site` | string | `precheck` / `validating` / `jenkins` / `deploy` / … |
| `service` | string \| null | 相关服务名 |
| `config_path` | string \| null | Compose 路径，如 `services.ai-worker.build.context` |
| `stage` / `location` / `reason` / `exit_code` / `jenkins_build_number` | 可选 | Jenkins 现场信息 |

### `code` 取值

| code | 含义 |
| --- | --- |
| `COMPOSE_CONFIG_ERROR` | Compose / 构建上下文 / Dockerfile 等配置类错误 |
| `JENKINS_BUILD_ERROR` | Jenkins 构建期错误（网络、依赖、编译等） |

### 已支持 `subcode`

#### Compose 配置类（`COMPOSE_CONFIG_ERROR`）

| subcode | 典型触发 | remediation 要点 |
| --- | --- | --- |
| `UNSUPPORTED_GIT_BIND_MOUNT` | Git 源使用相对 bind mount | 改用 Dockerfile `COPY` 或命名卷 |
| `BUILD_CONTEXT_MISSING` | `build context does not exist` | 检查 `build.context` 目录是否存在于源码/workspace |
| `DOCKERFILE_MISSING` | Dockerfile 不存在 | 检查 `build.dockerfile` 相对 context 的路径与大小写 |
| `WORKSPACE_PATH_MISSING` | workspace 路径不存在 | 确认上传内容完整 |
| `LOCAL_BUILD_REQUIRED` | 缺少 local build 定义 | 必须提供 `build.context` + `build.dockerfile` |
| `UNSAFE_BUILD_PATH` | 绝对路径 / `..` 逃逸 | 改用安全相对 POSIX 路径 |

#### Jenkins 构建类（`JENKINS_BUILD_ERROR`）

| subcode | 典型触发 |
| --- | --- |
| `DNS_RESOLUTION_FAILED` | could not resolve host / ENOTFOUND |
| `CONNECTION_FAILED` | connection refused / timed out |
| `TLS_CERTIFICATE_FAILED` | x509 / SSL certificate problem |
| `AUTH_OR_PERMISSION_FAILED` | 401/403 / unauthorized |
| `PACKAGE_REGISTRY_REQUEST_FAILED` | registry.npmjs 等请求失败 |
| `RUNTIME_ENGINE_INCOMPATIBLE` | EBADENGINE |
| `FILE_OR_PATH_MISSING` | ENOENT / cannot find module |
| `DISK_SPACE_EXHAUSTED` | ENOSPC |
| `PORT_CONFLICT` | address already in use |
| `UNCLASSIFIED_CONSOLE_ERROR` | 已定位控制台行但未映射更细子码 |
| `UNKNOWN` | 日志中未识别具体错误（仍应有 raw_log） |
| `CONSOLE_UNAVAILABLE` | 诊断解析失败；若有 console 尾部仍会回传 raw_log |

---

## 四、示例：Validate Compose / build context 缺失

线上案例形态（Jenkins `#144`，阶段 `Validate Compose`）：

```json
{
  "status": "failed",
  "failure_site": "jenkins",
  "error": "Jenkins build finished with FAILURE；Jenkins #144；阶段=Validate Compose；原因=构建上下文缺失：ERROR: service ai-worker build context does not exist: ./backend；建议=请检查 compose 中该服务的 build.context…",
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",
    "subcode": "BUILD_CONTEXT_MISSING",
    "cause": "构建上下文缺失",
    "service": "ai-worker",
    "config_path": "services.ai-worker.build.context",
    "recognized": true,
    "failure_site": "jenkins",
    "stage": "Validate Compose",
    "jenkins_build_number": 144,
    "remediation": "请检查 compose 中该服务的 build.context 是否指向源码仓库或 workspace 内真实存在的目录；例如 context 为 ./backend 时，源码根目录下必须有 backend/。",
    "summary": "阶段=Validate Compose；…；原因=构建上下文缺失：…"
  },
  "raw_log_excerpt": "  L1: Running on builder-01 …\n>> L4: ERROR: service ai-worker build context does not exist: ./backend\n…",
  "failure_logs": {
    "source": "jenkins_console",
    "raw_log_excerpt": "…"
  }
}
```

APPS 判定建议：

```python
def is_user_fixable_compose_error(build: dict) -> bool:
    detail = build.get("error_detail") or {}
    return detail.get("code") == "COMPOSE_CONFIG_ERROR"
```

---

## 五、脱敏策略（与旧文档差异）

| 通道 | 策略 |
| --- | --- |
| `raw_log_excerpt` | 仅剥离 ANSI / 控制序列；**不** scrub 应用环境变量与 compose 值 |
| `error` / alert `message` / `error_detail.reason` | 仍脱敏已知应用环境值、URL 凭据、Bearer/Basic、私钥等 |

原因：失败诊断面向应用开发者；平台侧告警仍避免把秘密扩散到事件流。

---

## 六、兼容与迁移

- 历史失败记录不回填；仅新失败写入完整契约。
- 旧字段 `error` / `failure_site` / `recognized` 保留。
- `recognized=true` **不等于** Compose 配置错误；请以 `code == "COMPOSE_CONFIG_ERROR"` 为准。
- 不要再把缺失 `code` 映射成面向用户的「请联系平台排查」；应展示 `raw_log_excerpt` + `remediation`。

---

## 七、配置

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `ORCHESTRATOR_FAILURE_SUMMARY_MAX_CHARS` | 4000 | `error` / 告警摘要上限 |
| `ORCHESTRATOR_FAILURE_LOG_EXCERPT_MAX_CHARS` | 32000 | `raw_log_excerpt` 上限 |
| `ORCHESTRATOR_JENKINS_CONSOLE_WINDOW_BYTES` | 262144 | Jenkins console 读取窗口 |
