# APPS Integration Guide: Batch Environment Variable Validation

## 概述

本文档描述从commit `f82807f`开始生效的Compose环境变量批量验证机制API变化。

**核心变化**：
- build_create在创建build记录前进行同步批量门禁检查
- 缺失环境变量时返回HTTP 422，包含结构化`error_detail`
- **一次列出所有**缺失/空值的必需变量（不再逐个报错）
- worker validating阶段保留异步防御层

## API端点

### POST /api/v1/jenkins/build_create/{app_id}

**请求体**：
```json
{
  "git_ref": "main"  // 可选，默认使用app.git_ref
}
```

**成功响应** (HTTP 202 Accepted):
```json
{
  "build_id": "abc123...",
  "appid": "APP-...",
  "status": "queued",
  ...
}
```

**失败响应 - 缺失环境变量** (HTTP 422 Unprocessable Entity):
```json
{
  "detail": "Compose配置缺失3个必需的环境变量: DATABASE_URL, API_KEY, SECRET_TOKEN。请在应用环境设置中配置这些变量后重试。",
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",
    "subcode": "MISSING_ENVIRONMENT_VARIABLES",
    "summary": "Compose配置缺失3个必需的环境变量: DATABASE_URL, API_KEY, SECRET_TOKEN。请在应用环境设置中配置这些变量后重试。",
    "remediation": "DATABASE_URL: 在Compose中引用但未配置; API_KEY: 在Compose中引用但未配置; SECRET_TOKEN: 配置为空字符串但Compose要求非空值。",
    "recognized": true,
    "failure_site": "precheck",
    "check_scope": "services.environment",
    "missing_variables": [
      {
        "name": "DATABASE_URL",
        "reason": "missing",
        "locations": [
          {
            "service": "web",
            "environment_key": "DATABASE_URL",
            "config_path": "services.web.environment"
          },
          {
            "service": "api",
            "environment_key": "DB_URL",
            "config_path": "services.api.environment"
          }
        ]
      },
      {
        "name": "API_KEY",
        "reason": "missing",
        "locations": [
          {
            "service": "web",
            "environment_key": "API_KEY",
            "config_path": "services.web.environment"
          }
        ]
      },
      {
        "name": "SECRET_TOKEN",
        "reason": "empty",
        "locations": [
          {
            "service": "worker",
            "environment_key": "SECRET_TOKEN",
            "config_path": "services.worker.environment"
          }
        ]
      }
    ]
  }
}
```

## 字段说明

### error_detail 顶层字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | string | 固定值 `"COMPOSE_CONFIG_ERROR"` |
| `subcode` | string | 固定值 `"MISSING_ENVIRONMENT_VARIABLES"` |
| `summary` | string | 人类可读的错误摘要 |
| `remediation` | string | 补救建议，列举所有缺失变量及原因 |
| `recognized` | boolean | 固定值 `true`，表示已识别的错误 |
| `failure_site` | string | 固定值 `"precheck"`（同步门禁） |
| `check_scope` | string | 固定值 `"services.environment"` |
| `missing_variables` | array | 缺失变量详细信息数组 |

### missing_variables 数组元素

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 环境变量名（如 `DATABASE_URL`） |
| `reason` | string | `"missing"`（未配置）或 `"empty"`（配置为空但要求非空） |
| `locations` | array | 该变量在Compose中的所有引用位置 |

### locations 数组元素

| 字段 | 类型 | 说明 |
|------|------|------|
| `service` | string | 引用此变量的服务名 |
| `environment_key` | string | Compose environment中的键名 |
| `config_path` | string | Compose配置路径（如 `services.web.environment`） |

## 聚合规则

同一变量在多处引用时：
- `missing_variables`数组中**仅出现一次**该变量
- `locations`数组包含**所有引用位置**
- 按**首次出现顺序**排列（稳定排序）

示例：`DATABASE_URL`在web、api、worker三个服务中引用，返回一个条目包含三个locations。

## reason 值详解

| reason | 含义 | Compose语法示例 | 
|--------|------|-----------------|
| `"missing"` | 变量未在app.environment中配置 | `${VAR}`, `${VAR:?hint}`, `${VAR?hint}` |
| `"empty"` | 变量已配置但值为空字符串，而Compose要求非空 | `${VAR:?hint}`, `${VAR:-default}` |

**注意**：
- `:?` 要求非空值（empty string不通过）
- `?` 仅要求存在（empty string通过）
- `:-` 有默认值但检查空值
- `-` 有默认值且不检查空值

## 安全保障

**验证器绝不返回以下内容**：
- 实际环境变量值
- Compose默认表达式（`${VAR:-default}`中的`default`）
- 用户自定义hint（`${VAR:?hint}`中的`hint`）
- 插值结果或原始环境输入

**日志与异常同样脱敏**，不会泄露secret。

## 支持的Compose语法

| 语法 | 示例 | 行为 |
|------|------|------|
| 标准 | `${VAR}` | 必需，缺失则失败 |
| 默认值 | `${VAR:-default}` | 可选，缺失或空时用default |
| 仅缺失默认 | `${VAR-default}` | 可选，仅缺失时用default（空值不用） |
| 必需非空 | `${VAR:?hint}` | 必需非空，缺失或空则失败 |
| 必需存在 | `${VAR?hint}` | 必需存在，缺失则失败（空值通过） |
| 转义 | `$$` | 字面量`$`，不是变量 |
| 裸语法 | `$VAR` | 必需，等同于`${VAR}` |

## PATCH /api/v1/jenkins/app_info/{app_id} 行为

**环境变量更新语义**：
```json
{
  "environment": {
    "NEW_VAR": "new_value",
    "UPDATE_VAR": "updated_value"
  }
}
```

**重要**：PATCH使用**替换语义**，不是合并。
- 未在PATCH中提供的现有变量**保持不变**
- 要删除变量，需显式传递所有保留的变量（类似PUT）

**后端逻辑**：
```python
# 解密现有环境
current_env = decrypt_environment(app.environment_ciphertext)
# PATCH提供的新环境
new_env = request.environment
# 合并逻辑（后端实现）
merged_env = {**current_env, **new_env}
# 加密并保存
app.environment_ciphertext = encrypt_environment(merged_env)
```

**一次性修复所有缺失变量**：
1. POST build_create获取422错误及missing_variables数组
2. 提取所有`name`字段：`["VAR1", "VAR2", "VAR3"]`
3. 让用户输入每个变量的值
4. PATCH app_info一次性提交所有变量
5. 再次POST build_create即可成功

## 预检验证端点（可选未来扩展）

**注意**：当前版本未提供独立预检端点。
如需在表单提交前验证，可调用POST build_create并检查HTTP状态码：
- 422 → 显示missing_variables列表让用户填写
- 202 → 验证通过（build已创建，无法取消）

未来可能新增：
```
POST /api/v1/jenkins/validate_environment/{app_id}
```
仅验证不创建build，返回相同error_detail结构。

## 向后兼容

| 字段 | 旧版本 | 新版本 |
|------|--------|--------|
| `detail` | ✓ 存在 | ✓ 保持不变 |
| `error_detail.code` | ✗ 不存在 | ✓ 新增 |
| `error_detail.subcode` | ✗ 不存在 | ✓ 新增 |
| `error_detail.summary` | ✓ 存在 | ✓ 保持不变 |
| `error_detail.recognized` | ✓ 存在 | ✓ 保持不变 |
| `error_detail.failure_site` | ✓ 存在 | ✓ 保持不变 |
| `error_detail.missing_variables` | ✗ 不存在 | ✓ 新增 |

**旧版APPS客户端**：
- 继续使用`detail`字符串显示错误
- 忽略`missing_variables`字段
- 行为不变

**新版APPS客户端**：
- 解析`missing_variables`数组
- 显示结构化表单让用户一次性填写所有缺失变量
- 提供更好的用户体验

## 检查范围限制

| 检查项 | 是否检查 | 说明 |
|--------|---------|------|
| `services.*.environment` | ✓ | 检查 |
| `services.*.build.args` | ✗ | 不检查（构建时参数，不是运行时环境） |
| `env_file` | ✗ | 不检查（文件内容动态加载） |
| 非services字段 | ✗ | 不检查 |

## 部署前后行为对比

### 部署前（< commit f82807f）

**缺失单个变量**：
```bash
curl -X POST /api/v1/jenkins/build_create/APP-123
→ HTTP 202 (build创建成功)
→ Jenkins构建失败
→ error_detail: "references missing...DATABASE_URL"
```

**缺失多个变量**：
```bash
第一次构建 → 失败："references missing...VAR1"
配置VAR1 → 第二次构建 → 失败："references missing...VAR2"
配置VAR2 → 第三次构建 → 失败："references missing...VAR3"
...（需要多次试错）
```

### 部署后（>= commit f82807f）

**缺失单个或多个变量**：
```bash
curl -X POST /api/v1/jenkins/build_create/APP-123
→ HTTP 422 (build未创建)
→ error_detail.missing_variables: ["VAR1", "VAR2", "VAR3"]
```

**用户一次性配置所有变量后成功**：
```bash
curl -X PATCH /api/v1/jenkins/app_info/APP-123 \
  -d '{"environment": {"VAR1": "v1", "VAR2": "v2", "VAR3": "v3"}}'
→ HTTP 200

curl -X POST /api/v1/jenkins/build_create/APP-123
→ HTTP 202 (成功)
```

## 实际案例

### 案例1：APP-20261118-27C5BCB7 scm-test3

**背景**：
- Repository SHA: `ec114efcba9151931c17109c2e1afd90ae637860`
- 首次构建失败：`BOOTSTRAP_ADMIN_PASSWORD_HASH`
- 配置后第二次构建失败：`BUSINESS_MEILI_MASTER_KEY`
- 实际需要6个变量

**部署前**（旧行为）：
```
构建1 → 缺失 BOOTSTRAP_ADMIN_PASSWORD_HASH
构建2 → 缺失 BUSINESS_MEILI_MASTER_KEY
构建3 → （可能还有更多缺失变量）
```

**部署后**（新行为）：
```json
POST /build_create/APP-20261118-27C5BCB7
→ HTTP 422
{
  "error_detail": {
    "missing_variables": [
      {"name": "MONGO_URI", "reason": "missing", ...},
      {"name": "MEILI_MASTER_KEY", "reason": "missing", ...},
      {"name": "BUSINESS_MEILI_MASTER_KEY", "reason": "missing", ...},
      {"name": "SESSION_SECRET", "reason": "missing", ...},
      {"name": "BOOTSTRAP_ADMIN_PASSWORD_HASH", "reason": "missing", ...},
      {"name": "EXT_APP_FERNET_KEY", "reason": "missing", ...}
    ]
  }
}
```

用户一次性配置6个变量后，下次构建直接成功。

## 错误处理最佳实践

### 前端表单集成

```typescript
async function createBuild(appId: string, gitRef?: string) {
  try {
    const response = await fetch(`/api/v1/jenkins/build_create/${appId}`, {
      method: 'POST',
      body: JSON.stringify({ git_ref: gitRef }),
    });
    
    if (response.status === 422) {
      const error = await response.json();
      if (error.error_detail?.subcode === 'MISSING_ENVIRONMENT_VARIABLES') {
        // 显示表单让用户填写缺失变量
        const missingVars = error.error_detail.missing_variables;
        return showEnvironmentForm(appId, missingVars);
      }
    }
    
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${error.detail}`);
    }
    
    return await response.json();
  } catch (err) {
    console.error('Build creation failed:', err);
    throw err;
  }
}

function showEnvironmentForm(appId: string, missingVars: any[]) {
  // 渲染表单
  const form = document.createElement('form');
  
  missingVars.forEach(varInfo => {
    const field = document.createElement('div');
    field.innerHTML = `
      <label>${varInfo.name}</label>
      <input name="${varInfo.name}" required />
      <small>
        ${varInfo.reason === 'missing' ? '未配置' : '配置为空'}
        | 引用位置: ${varInfo.locations.map(l => l.service).join(', ')}
      </small>
    `;
    form.appendChild(field);
  });
  
  form.onsubmit = async (e) => {
    e.preventDefault();
    const formData = new FormData(form);
    const environment = Object.fromEntries(formData);
    
    // 更新环境变量
    await fetch(`/api/v1/jenkins/app_info/${appId}`, {
      method: 'PATCH',
      body: JSON.stringify({ environment }),
    });
    
    // 重新尝试创建build
    createBuild(appId);
  };
  
  return form;
}
```

### 命令行工具集成

```bash
# 获取缺失变量列表
missing=$(curl -s -X POST /api/v1/jenkins/build_create/APP-123 | \
  jq -r '.error_detail.missing_variables[].name')

if [ -n "$missing" ]; then
  echo "请配置以下环境变量："
  echo "$missing"
  
  # 交互式输入
  declare -A env_vars
  for var in $missing; do
    read -sp "请输入 $var: " value
    env_vars[$var]="$value"
    echo
  done
  
  # 一次性更新
  jq -n --argjson vars "$(declare -p env_vars | sed 's/declare -A env_vars=//')" \
    '{environment: $vars}' | \
    curl -X PATCH /api/v1/jenkins/app_info/APP-123 -d @-
  
  # 重新创建build
  curl -X POST /api/v1/jenkins/build_create/APP-123
fi
```

## 常见问题

### Q1: 为什么有时reason是"empty"？

A: 当变量在app.environment中配置为空字符串`""`，但Compose使用`:?`或`:-`操作符要求非空值时。

```yaml
# docker-compose.yml
environment:
  - API_KEY=${API_KEY:?Required}  # 要求非空

# app.environment
{"API_KEY": ""}  # 空字符串

# 结果
reason: "empty"
```

### Q2: locations数组为什么有多个元素？

A: 同一变量可能在多处引用：
- 多个服务引用相同变量
- 同一服务的多个environment字段引用（虽然少见）
- 不同environment_key映射到同一变量名

示例：
```yaml
services:
  web:
    environment:
      - DATABASE_URL=${DB_URL}
  api:
    environment:
      - DB_CONNECTION=${DB_URL}
```

两处都缺失`DB_URL`，返回一个变量条目包含两个locations。

### Q3: 如何知道build未创建？

A: HTTP 422响应时，build记录**未创建**，Jenkins未触发。
HTTP 202响应时，build记录**已创建**，Jenkins已排队。

### Q4: 异步validating阶段还会检查吗？

A: 是的。worker validating阶段保留相同验证逻辑作为**防御纵深**。
这是为了防止：
- 同步检查与异步执行之间的环境变量被修改
- 未知边缘情况导致的检查遗漏
- 时序问题

### Q5: 如果Compose使用`env_file`呢？

A: 当前版本**不检查**`env_file`引用的变量，因为：
- 文件内容在构建时动态加载
- 无法静态分析文件中定义的变量
- 可能包含大量变量，不适合全部要求预配置

仅检查`services.*.environment`中的显式引用。

## 迁移建议

### 对于APPS前端

1. **显示友好错误提示**
   ```javascript
   if (error.error_detail?.subcode === 'MISSING_ENVIRONMENT_VARIABLES') {
     const varList = error.error_detail.missing_variables
       .map(v => v.name)
       .join(', ');
     showError(`缺失环境变量: ${varList}`);
   }
   ```

2. **提供一键修复表单**
   - 解析`missing_variables`数组
   - 渲染输入框让用户填写
   - 一次性PATCH所有变量
   - 自动重试build_create

3. **缓存检查结果**
   - build_create失败后，缓存missing_variables
   - 用户修复环境变量后，清除缓存
   - 避免重复调用

### 对于后端集成

1. **解析error_detail结构**
   ```python
   if response.status_code == 422:
       error_detail = response.json().get('error_detail', {})
       if error_detail.get('subcode') == 'MISSING_ENVIRONMENT_VARIABLES':
           missing = error_detail['missing_variables']
           # 处理缺失变量
   ```

2. **批量更新API调用**
   ```python
   def fix_missing_vars(app_id, missing_vars):
       env_update = {}
       for var in missing_vars:
           value = prompt_user(var['name'], var['reason'])
           env_update[var['name']] = value
       
       patch_app_environment(app_id, env_update)
   ```

## 测试验证

### 测试覆盖

- ✓ 18个单元测试 (test_compose_validator.py)
- ✓ 13个集成测试 (test_compose_integration.py)
- ✓ 34个现有测试保持通过 (test_workspaces.py)
- ✓ 实际案例scm-test3覆盖

### 手动测试清单

- [ ] 缺失单个变量 → HTTP 422，error_detail包含1个变量
- [ ] 缺失多个变量 → HTTP 422，error_detail包含所有变量
- [ ] 空值与:?操作符 → reason为"empty"
- [ ] 有默认值的变量 → 不出现在missing_variables
- [ ] PATCH更新后重试 → HTTP 202成功
- [ ] 旧APPS客户端 → 继续使用detail字段，无破坏性变化

## 性能影响

**同步门禁增加延迟**：
- 解密environment: ~1-5ms
- 解析Compose: ~1-10ms（取决于services数量）
- 正则匹配所有变量引用: ~0.1ms per service
- 总延迟: 通常 < 20ms

**权衡**：
- 门禁检查增加少量延迟（< 20ms）
- 避免无效构建节省Jenkins资源和用户等待时间
- 减少用户试错次数（从N次降到1次）

## 限制与未来扩展

### 当前限制

1. **仅检查services.environment**
   - 不检查`build.args`
   - 不检查`env_file`引用
   - 不检查非services字段

2. **不递归展开默认值中的变量**
   ```yaml
   # 不支持嵌套展开
   VAR1: ${VAR2:-${VAR3:-default}}
   ```

3. **不支持Compose文件中的扩展字段引用**
   ```yaml
   x-common: &common
     environment:
       - VAR=${VAR}
   
   services:
     web:
       <<: *common  # 当前不解析YAML锚点
   ```

### 未来可能扩展

1. **独立预检端点**
   ```
   POST /api/v1/jenkins/validate_environment/{app_id}
   ```
   仅验证不创建build，用于表单实时检查。

2. **支持env_file变量检查**
   - 需要文件内容访问权限
   - 性能影响需评估

3. **支持YAML锚点和扩展字段**
   - 需要完整YAML解析器
   - 当前用简化JSON处理

4. **批量修复建议API**
   ```
   GET /api/v1/jenkins/suggest_env_fix/{app_id}
   ```
   返回缺失变量及建议值（非敏感变量）。

## 相关文档

- [Compose Error Classification](./APPS_INTEGRATION_GUIDE.md) - Phase 1错误分类机制
- [Docker Compose Variable Interpolation](https://docs.docker.com/compose/environment-variables/variable-interpolation/) - 官方文档
- [Build Input Error Handling](./BUILD_ERROR_HANDLING.md) - 构建错误处理完整指南

## 变更历史

| 版本 | Commit | 日期 | 变化 |
|------|--------|------|------|
| 1.0 | f82807f | 2026-11-20 | 首次发布批量验证机制 |

## 支持与反馈

- GitHub Issues: [zl875136491/docker-jenkins-orchestrator]
- 文档问题: 提交PR到 `docs/APPS_INTEGRATION_ENV_VALIDATION.md`
- 集成问题: 检查`error_detail.missing_variables`结构是否完整
