# APPS 构建错误契约

本文档为APPS开发者提供统一的构建错误分类契约，用于判断错误类别、责任方、是否需要用户操作以及推荐的处理规则。

## 概述

构建失败时，API响应和BuildJob记录的`error_detail`字段包含结构化的错误信息，支持机器判断错误分类和处理策略。

**核心字段**：
- `code`: 错误代码（如`COMPOSE_CONFIG_ERROR`）
- `subcode`: 错误子类型（如`MISSING_ENVIRONMENT_VARIABLES`）
- `category`: 错误类别（`configuration_error`/`unknown_error`等）
- `responsible_party`: 责任方（`application_developer`/`platform`/`unknown`）
- `retryable`: 是否可重试（`true`/`false`）
- `user_action_required`: 是否需要用户操作（`true`/`false`）
- `action_guidance`: 中文操作指引
- `summary`: 中文错误摘要
- `remediation`: 中文修复建议（部分错误类型）
- `validation_warnings`: 非阻断警告列表（可选，仅当检测到外部依赖等情况时出现）

## 错误类别（category）

| 类别 | 值 | 说明 |
|------|-----|------|
| 配置错误 | `configuration_error` | 应用配置问题，需修改Compose/环境变量 |
| 未知错误 | `unknown_error` | 需查看日志判断原因 |

**说明**：
- 只有明确证据的失败才归类为`configuration_error`
- 大部分失败保守归类为`unknown_error`，需查看日志判断

## 责任方（responsible_party）

| 责任方 | 值 | 说明 |
|--------|-----|------|
| 应用开发者 | `application_developer` | 需修改应用配置或代码 |
| 平台 | `platform` | 平台问题，联系平台排查 |
| 未知 | `unknown` | 需查看日志判断 |

## 已识别的配置错误类型

### 1. 缺失环境变量

**识别字段**：
```json
{
  "code": "COMPOSE_CONFIG_ERROR",
  "subcode": "MISSING_ENVIRONMENT_VARIABLES",
  "category": "configuration_error",
  "responsible_party": "application_developer",
  "missing_variables": [
    {
      "name": "DATABASE_URL",
      "reason": "missing",
      "locations": [
        {
          "service": "web",
          "environment_key": "DATABASE_URL"
        }
      ]
    }
  ]
}
```

**处理规则**：
- 补充`missing_variables`列表中的环境变量
- 通过应用配置界面添加环境变量
- 重新创建构建

### 2. 外部依赖端点检测（非阻断警告）

**新行为**（2026-11-25起）：外部业务数据库引用不再默认阻断构建，而是生成非阻断警告。

**validation_warnings字段**：
```json
{
  "build_id": "abc123",
  "status": "building",
  "validation_warnings": [
    {
      "code": "EXTERNAL_DEPENDENCY_DETECTED",
      "severity": "warning",
      "blocking": false,
      "message": "检测到外部依赖端点配置",
      "environment_key": "ECN_DB_HOST",
      "locations": [
        {"service": "api", "key": "ECN_DB_HOST"},
        {"service": "worker", "key": "ECN_DB_HOST"}
      ]
    }
  ]
}
```

**说明**：
- `validation_warnings`出现时，构建正常继续（非阻断）
- 警告仅供参考，不影响构建成功
- 不会生成环境变量修复表单（warnings不是errors）
- 检测范围：启发式匹配`*_HOST`/`*_URL`/`*_URI`等端点变量
- 检测非全量：文档承认无法覆盖所有业务数据库变量

**历史行为（保留用于兼容）**：
某些情况下，外部端点仍会导致`EXTERNAL_STATEFUL_ENDPOINT`错误（如旧版本行为或明确的隔离违规）。处理规则：
- 在同一构建的`services`中定义数据库服务
- 使用服务名而非外部URI连接

### 3. 端点格式无效（阻断错误）

**识别字段**：
```json
{
  "code": "COMPOSE_CONFIG_ERROR",
  "subcode": "INVALID_STATEFUL_ENDPOINT",
  "category": "configuration_error",
  "responsible_party": "application_developer",
  "endpoint_variable": "DATABASE_URL",
  "summary": "服务配置中端点格式无效"
}
```

**触发条件**：
- 端口号非数字（如`postgres://user:pass@host:badport/db`）
- 主机名格式无效
- URI语法错误

**处理规则**：
- 检查连接字符串的主机名和端口格式是否正确
- 确保端口号为有效数字
- 不回显完整URI（安全考虑）

### 4. 不支持的Git源码绑定挂载

**识别字段**：
```json
{
  "code": "COMPOSE_CONFIG_ERROR",
  "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",
  "category": "configuration_error",
  "responsible_party": "application_developer"
}
```

**处理规则**：
- 修改Compose配置，使用Dockerfile `COPY`指令
- 或使用命名Docker卷替代bind mount

## 未知错误处理

**识别字段**：
```json
{
  "category": "unknown_error",
  "responsible_party": "unknown",
  "recognized": false
}
```

**处理规则**：
1. 查看`summary`字段的中文摘要
2. 查看`action_guidance`字段的操作指引
3. 检查构建日志（`failure_logs`/`raw_log_excerpt`）判断具体原因
4. 如无法判断，联系平台排查

## API接口

### HTTP 422响应（同步门禁）

**POST** `/api/v1/jenkins/build_create/{app_id}`

创建构建时，如果配置错误，返回HTTP 422：

```json
{
  "detail": "Compose配置缺失1个必需的环境变量: DATABASE_URL",
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",
    "subcode": "MISSING_ENVIRONMENT_VARIABLES",
    "category": "configuration_error",
    "responsible_party": "application_developer",
    "retryable": false,
    "user_action_required": true,
    "action_guidance": "补充缺失的环境变量后重新创建构建",
    "summary": "Compose配置缺失1个必需的环境变量: DATABASE_URL",
    "missing_variables": [...]
  }
}
```

### BuildJob查询（异步失败）

**GET** `/api/v1/jenkins/build_info/{build_id}`

构建失败时，BuildJob的`error_detail`字段包含相同结构：

```json
{
  "build_id": "abc123",
  "status": "failed",
  "failure_site": "validating",
  "error": "Compose配置缺失必需的环境变量",
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",
    "subcode": "MISSING_ENVIRONMENT_VARIABLES",
    "category": "configuration_error",
    "responsible_party": "application_developer",
    ...
  }
}
```

## 时序范围

错误分类覆盖以下阶段：

| 阶段 | failure_site | 说明 |
|------|--------------|------|
| 同步检查 | `precheck` | HTTP 422，构建未创建 |
| 异步验证 | `validating` | Worker验证阶段 |
| 其他阶段 | `triggering`/`jenkins`/`deploy`等 | 大部分保守分类为unknown |

**重要**：
- 仅`precheck`和`validating`阶段的部分错误有明确分类
- 其他阶段错误多数为`unknown`，需查看日志判断

## 推荐处理规则

### 1. 判断是否可自动修复

```python
if error_detail.get("category") == "configuration_error" and \
   error_detail.get("responsible_party") == "application_developer":
    # 引导用户修复配置
    show_remediation(error_detail.get("action_guidance"))
else:
    # 显示错误摘要，建议查看日志或联系平台
    show_summary_and_logs(error_detail.get("summary"))
```

### 2. 处理历史记录

**旧记录（无`category`字段）**：
```python
if "category" not in error_detail:
    # 历史记录，按旧逻辑处理
    if error_detail.get("code") == "COMPOSE_CONFIG_ERROR":
        # 配置错误
    else:
        # 未知错误
```

### 3. 处理正在运行的构建

**queued/building状态**：
- 不显示错误分类
- `error_detail`为空或null
- 不要将正在运行的构建当作失败处理

### 4. 判断是否可重试

```python
if error_detail.get("retryable") is True:
    # 显示"重试"按钮
    enable_retry_button()
elif error_detail.get("user_action_required") is True:
    # 显示"修复配置"引导
    show_fix_config_guide(error_detail)
else:
    # 显示"查看日志"或"联系平台"
    show_logs_or_contact()
```

## 安全说明

- `error_detail`中不包含密钥、密码等敏感信息
- 外部端点错误不显示完整URI，只显示变量名
- 修复建议中的示例不含真实凭据
- `raw_log_excerpt`可能包含敏感信息，仅内部使用，不直接展示给最终用户

## 字段完整性保证

**保证不丢失的字段**：
- `code`, `subcode`: 保持不变
- `missing_variables`: 完整保留
- `summary`, `remediation`: 中文摘要和建议
- `recognized`: 是否已识别

**新增字段**：
- `category`, `responsible_party`, `retryable`, `user_action_required`, `action_guidance`

**兼容性**：
- 新字段不影响旧版本APPS（可忽略新字段）
- 旧字段完整保留，旧版本APPS仍可正常工作

## 示例代码

### TypeScript/JavaScript

```typescript
interface ErrorDetail {
  code?: string;
  subcode?: string;
  category?: string;
  responsible_party?: string;
  retryable?: boolean;
  user_action_required?: boolean;
  action_guidance?: string;
  summary: string;
  remediation?: string;
  recognized?: boolean;
  missing_variables?: Array<{
    name: string;
    reason: string;
    locations: Array<{service: string; environment_key: string}>;
  }>;
}

function handleBuildError(error_detail: ErrorDetail) {
  // 判断是否为配置错误
  if (error_detail.category === "configuration_error" && 
      error_detail.responsible_party === "application_developer") {
    // 显示修复指引
    return {
      type: "fix_config",
      message: error_detail.action_guidance || error_detail.summary,
      fixable: true
    };
  }
  
  // 未知错误或历史记录
  return {
    type: "unknown",
    message: error_detail.summary,
    fixable: false,
    action: "查看日志或联系平台排查"
  };
}
```

### Python

```python
def handle_build_error(error_detail: dict) -> dict:
    """处理构建错误"""
    
    # 判断是否为已知配置错误
    if (error_detail.get("category") == "configuration_error" and
        error_detail.get("responsible_party") == "application_developer"):
        return {
            "type": "fix_config",
            "message": error_detail.get("action_guidance") or error_detail.get("summary"),
            "fixable": True
        }
    
    # 未知错误
    return {
        "type": "unknown",
        "message": error_detail.get("summary", "构建失败"),
        "fixable": False,
        "action": "查看日志或联系平台排查"
    }
```

## 变更历史

- 2026-11-25: 外部依赖检测改为非阻断warnings；新增INVALID_STATEFUL_ENDPOINT错误类型；更新环境变量解析逻辑
- 2026-11-23: 初始版本，支持配置错误分类
