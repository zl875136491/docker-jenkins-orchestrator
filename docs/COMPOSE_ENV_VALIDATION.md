# Compose环境变量批量验证

## API变化

### POST /api/v1/jenkins/build_create/{app_id}

**成功**（HTTP 202）：
```json
{
  "build_id": "abc123...",
  "appid": "APP-...",
  "status": "queued"
}
```

**失败 - 缺失环境变量**（HTTP 422）：
```json
{
  "detail": "Compose配置缺失3个必需的环境变量: VAR1, VAR2, VAR3。请配置后重试。",
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",
    "subcode": "MISSING_ENVIRONMENT_VARIABLES",
    "summary": "Compose配置缺失3个必需的环境变量...",
    "recognized": true,
    "failure_site": "precheck",
    "check_scope": "services.environment",
    "missing_variables": [
      {
        "name": "VAR1",
        "reason": "missing",
        "locations": [
          {
            "service": "web",
            "environment_key": "VAR1",
            "config_path": "services.web.environment"
          }
        ]
      }
    ]
  }
}
```

## 字段说明

### missing_variables数组元素

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 变量名 |
| reason | string | "missing"（未配置）或"empty"（配置为空但要求非空） |
| locations | array | 该变量在Compose中的所有引用位置 |

### locations数组元素

| 字段 | 说明 |
|------|------|
| service | 引用此变量的服务名 |
| environment_key | Compose environment中的键名 |
| config_path | Compose配置路径 |

## Compose操作符语义

| 语法 | 示例 | 缺失 | 空值 |
|------|------|------|------|
| 标准 | `${VAR}` | 失败 | 通过 |
| 必需非空 | `${VAR:?hint}` | 失败 | 失败 |
| 必需存在 | `${VAR?hint}` | 失败 | 通过 |
| 默认值 | `${VAR:-default}` | 通过 | 通过（用default） |
| 仅缺失默认 | `${VAR-default}` | 通过 | 通过（用空值） |
| 转义 | `$$` | - | - |
| 裸语法 | `$VAR` | 失败 | 通过 |

**关键区别**：
- `:-` 在空值时使用默认值（不报错）
- `:?` 在空值时失败（报错）

## 验证范围

**检查**：
- `services.*.environment`中的`${VAR}`引用

**不检查**：
- `services.*.build.args`
- `env_file`引用
- 非services字段

## 验证时机

1. **同步门禁**：queue_build创建build记录前
   - 位置：静态Agent检查之后（保留错误优先级）
   - 失败：HTTP 422，无build记录，无Jenkins派发

2. **异步防御**：worker validating阶段
   - 使用相同验证器（避免规则漂移）
   - 防御纵深：检测同步/异步间的环境变化

## 安全保障

绝不返回：
- 实际环境变量值
- 默认值表达式（`${VAR:-default}`中的default）
- 用户hint（`${VAR:?hint}`中的hint）
- 插值结果

## 向后兼容

| 字段 | 旧版本 | 新版本 |
|------|--------|--------|
| detail | 存在 | 保持 |
| error_detail.summary | 存在 | 保持 |
| error_detail.recognized | 存在 | 保持 |
| error_detail.missing_variables | 不存在 | 新增 |

**时序变化**（不兼容）：
- 旧版本：HTTP 202 → Jenkins失败
- 新版本：HTTP 422（同步拒绝）

APPS需同时处理两种结果。

## 实际案例

APP-20261118-27C5BCB7 scm-test3（SHA ec114ef）：

**旧版本**：
```
构建1 → 失败：BOOTSTRAP_ADMIN_PASSWORD_HASH
构建2 → 失败：BUSINESS_MEILI_MASTER_KEY
...（需多次试错）
```

**新版本**：
```json
POST /build_create → HTTP 422
{
  "missing_variables": [
    {"name": "MONGO_URI", ...},
    {"name": "MEILI_MASTER_KEY", ...},
    {"name": "BUSINESS_MEILI_MASTER_KEY", ...},
    {"name": "SESSION_SECRET", ...},
    {"name": "BOOTSTRAP_ADMIN_PASSWORD_HASH", ...},
    {"name": "EXT_APP_FERNET_KEY", ...}
  ]
}
```

用户一次性配置6个变量后成功。

## 修复历史

**Commit f82807f**（初始实现）：
- 批量验证器
- 同步+异步门禁

**Commit 0b62eee**（缺陷修复）：
- 修复`:-`空值误报
- 复用DockerSwarmAdapter._environment
- 调整门禁位置（静态检查之后）

## 测试证据

```bash
$ pytest tests/test_compose_validator.py tests/test_workspaces.py -v
======================== 52 passed, 1 warning in 3.68s ========================
```

**核心用例**：
- 空值+`:-`通过（使用默认值）
- 空值+`:?`失败（要求非空）
- 同变量混合`:-`与`:?`：仅`:?`位置失败
- DockerSwarmAdapter._environment解析（与worker一致）

## 限制

1. 不检查env_file引用
2. 不支持YAML锚点（仅JSON parser）
3. 不递归展开默认值中的变量

## 文档历史

| 版本 | Commit | 日期 | 变化 |
|------|--------|------|------|
| 1.0 | f82807f | 2026-11-20 | 初始实现 |
| 1.1 | 0b62eee | 2026-11-20 | 核心缺陷修复 |
