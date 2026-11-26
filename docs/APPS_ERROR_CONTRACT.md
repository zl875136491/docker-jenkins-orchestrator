# APPS 构建失败错误契约

面向 APPS / conductor 接入。只描述对外字段、分类与处理建议。

---

## 1. 读取优先级

构建 `status == "failed"` 时：

1. `raw_log_excerpt`：失败现场日志摘录（优先展示）
2. `error_detail.code` + `error_detail.subcode`：稳定机器分类
3. `error_detail.summary` / `error_detail.remediation` / `error_detail.action_guidance`：中文说明与修复建议
4. `error`：与 `error_detail.summary` 对齐的短摘要
5. `failure_logs`：备用日志袋（可含同源 `raw_log_excerpt`）

不要解析英文或中文句子做分类。不要把 `validation_warnings` 当成失败原因；先看 `status` / `error`。

```python
def render_failure(build: dict) -> dict:
    detail = build.get("error_detail") or {}
    raw = build.get("raw_log_excerpt") or (build.get("failure_logs") or {}).get("raw_log_excerpt")
    return {
        "code": detail.get("code"),
        "subcode": detail.get("subcode"),
        "category": detail.get("category"),
        "responsible_party": detail.get("responsible_party"),
        "retryable": detail.get("retryable"),
        "user_action_required": detail.get("user_action_required"),
        "summary": detail.get("summary") or build.get("error"),
        "remediation": detail.get("remediation") or "",
        "action_guidance": detail.get("action_guidance") or "",
        "raw_log": raw,
        "recognized": bool(detail.get("recognized")),
    }
```

---

## 2. `error_detail` 字段

| 字段 | 说明 |
| --- | --- |
| `code` / `subcode` | 稳定错误码；未知时为 `UNKNOWN_CODE` / `UNKNOWN_SUBCODE` |
| `category` | `configuration_error` / `infrastructure_error` / `application_build_error` / `application_runtime_error` / `timeout_error` / `unknown_error` |
| `responsible_party` | `application_developer` / `platform` / `unknown` |
| `retryable` | 是否建议直接重试 |
| `user_action_required` | 是否需要用户改配置/代码 |
| `recognized` | 是否命中白名单已知组合 |
| `summary` | 中文摘要 |
| `remediation` | 中文修复建议 |
| `action_guidance` | 下一步动作说明 |
| `service` / `config_path` | 相关服务与 Compose 路径（若有） |
| `build_context_path` / `dockerfile_path` | 缺失路径（若有） |
| `missing_variables` | 仅 `MISSING_ENVIRONMENT_VARIABLES` |
| `failure_site` / `stage` / `jenkins_build_number` / `cause` / `location` | 现场上下文（若有） |

---

## 3. 已识别硬失败组合

| code | subcode | 责任方 | 用户操作 | APPS 建议 |
| --- | --- | --- | --- | --- |
| `COMPOSE_CONFIG_ERROR` | `MISSING_ENVIRONMENT_VARIABLES` | application_developer | 是 | 打开 `application_variables_required` / `UPDATE_APPLICATION_CONFIGURATION` |
| `COMPOSE_CONFIG_ERROR` | `INVALID_STATEFUL_ENDPOINT` | application_developer | 是 | 提示修正连接串格式 |
| `COMPOSE_CONFIG_ERROR` | `UNSUPPORTED_GIT_BIND_MOUNT` | application_developer | 是 | 改为 Dockerfile `COPY` 或命名卷 |
| `COMPOSE_CONFIG_ERROR` | `MISSING_BUILD_CONTEXT` | application_developer | 是 | 修正 `build.context` 或补齐目录；展示 `raw_log_excerpt` |
| `COMPOSE_CONFIG_ERROR` | `MISSING_DOCKERFILE` | application_developer | 是 | 修正 `build.dockerfile` 或补齐文件 |

只有 `MISSING_ENVIRONMENT_VARIABLES` 应驱动环境变量表单。不要把 warnings 或未知错误映射到该表单。

---

## 4. 示例：构建上下文缺失

```json
{
  "status": "failed",
  "failure_site": "jenkins",
  "error": "服务 ai-worker 的构建上下文不存在：./backend",
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",
    "subcode": "MISSING_BUILD_CONTEXT",
    "category": "configuration_error",
    "responsible_party": "application_developer",
    "retryable": false,
    "user_action_required": true,
    "recognized": true,
    "service": "ai-worker",
    "config_path": "services.ai-worker.build.context",
    "build_context_path": "./backend",
    "summary": "服务 ai-worker 的构建上下文不存在：./backend",
    "remediation": "请检查服务 ai-worker 的 build.context（当前为 ./backend）是否指向仓库或 workspace 内真实存在的目录；修正 Compose 路径或补齐缺失目录后重新构建。"
  },
  "raw_log_excerpt": "... ERROR: service ai-worker build context does not exist: ./backend ..."
}
```

---

## 5. 未知失败

`code=UNKNOWN_CODE`、`subcode=UNKNOWN_SUBCODE`、`recognized=false` 时：

- 仍应优先展示 `raw_log_excerpt`
- 不要忽略日志只展示笼统文案
- 责任方保守为 `unknown`

---

## 6. `validation_warnings`

非阻断提示（例如外部业务依赖检测）。可与后续真正失败同时存在。客户端必须先看 `status` / `error`，不要把 warnings 当失败原因。

---

## 7. 兼容说明

- 历史失败记录不回填；新契约只影响新构建
- `ENVIRONMENT_JSON={}` 在推送模型下是正常现象
- `raw_log_excerpt` 面向应用开发者诊断；APPS 可直接展示
