# Compose配置错误分类系统

## 概述

本文档描述构建系统的结构化Compose配置错误分类机制，用于准确区分Compose配置错误与基础设施/Jenkins故障。

## 版本历史

- **v2 (2026-11-17)**: 引入结构化错误分类，修复Git源bind mount误报
- **v1 (历史版本)**: 仅使用`recognized`标志和`failure_site`字段

## 错误分类体系

### 1. 结构化错误详情 (`error_detail`)

所有构建失败现在在`BuildJob.error_detail`字段中包含结构化的错误详情：

```json
{
  "code": "COMPOSE_CONFIG_ERROR",
  "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",
  "summary": "详细的错误描述",
  "service": "web",
  "config_path": "services.web.volumes",
  "remediation": "修复建议",
  "failure_site": "validating",
  "recognized": true
}
```

#### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `code` | string | 是 | 顶级错误代码，如`COMPOSE_CONFIG_ERROR` |
| `subcode` | string | 否 | 具体错误子类型，如`UNSUPPORTED_GIT_BIND_MOUNT` |
| `summary` | string | 是 | 用户可读的错误摘要 |
| `service` | string | 否 | 出错的服务名称 |
| `config_path` | string | 否 | 出错的配置路径（JSONPath风格） |
| `remediation` | string | 否 | 修复建议 |
| `failure_site` | string | 是 | 失败阶段：`precheck`, `validating`, `triggering`, `jenkins`, `deploy`等 |
| `recognized` | boolean | 是 | 是否为已识别的错误类型 |

### 2. Compose配置错误代码

#### `COMPOSE_CONFIG_ERROR` - Compose配置错误

**子代码 (subcode)**:

##### `UNSUPPORTED_GIT_BIND_MOUNT`
- **触发条件**: Git源构建使用相对路径bind mount
- **原因**: Git源文件在Jenkins阶段才checkout，validating阶段不存在
- **修复**: 使用Dockerfile `COPY`指令替代bind mount

##### `BUILD_CONTEXT_MISSING`
- **触发条件**: `service X build context does not exist: ./path`（Jenkins Validate Compose 或本地文件校验）
- **原因**: `build.context` 指向的目录在源码/workspace 中不存在
- **修复**: 修正 context 路径，或补齐缺失目录后重试

##### `DOCKERFILE_MISSING`
- **触发条件**: Dockerfile 相对 context 不存在
- **原因**: `build.dockerfile` 路径错误或未提交
- **修复**: 核对相对路径与大小写

**示例**:
```json
{
  "code": "COMPOSE_CONFIG_ERROR",
  "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",
  "summary": "Relative bind mount './data' is not supported for Git-based builds. Git source files are checked out during the Jenkins build stage, not in the workspace snapshot. To include files in the container, use COPY instructions in your Dockerfile instead of bind mounts.",
  "service": "redis",
  "config_path": "services.redis.volumes",
  "remediation": "Replace bind mounts with Dockerfile COPY instructions. For configuration files: COPY config/ /app/config/. For data persistence: use named Docker volumes instead of bind mounts.",
  "failure_site": "validating",
  "recognized": true
}
```

### 3. 非配置错误分类

并非所有`validating`阶段的错误都是Compose配置错误。系统会区分：

- **网络错误**: DNS解析失败、连接超时、TLS证书问题
- **认证错误**: Git仓库访问权限、Harbor注册表认证失败
- **基础设施错误**: 磁盘空间不足、OOM、Worker故障
- **Jenkins错误**: Jenkins构建失败、编译错误
- **Workspace错误**: Workspace上传限额、文件冲突

**非配置错误示例**:
```json
{
  "failure_site": "validating",
  "summary": "Unable to resolve Git repository DNS",
  "recognized": true,
  "exception_type": "GitLabError"
}
```

注意：此类错误**不会**设置`code: "COMPOSE_CONFIG_ERROR"`。

## HTTP API响应

### 同步创建构建 (HTTP 422)

当Compose配置在创建时可以确定无效时，API立即返回422：

```bash
POST /api/v1/jenkins/APP-Demo/builds
```

**响应 (422 Unprocessable Entity)**:
```json
{
  "detail": "Relative bind mount './data/redis:/data' is not supported for Git-based builds...",
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",
    "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",
    "summary": "...",
    "service": "redis",
    "config_path": "services.redis.volumes",
    "remediation": "Replace bind mounts with Dockerfile COPY instructions...",
    "recognized": true
  }
}
```

### 异步构建失败

如果配置错误在异步validating阶段检测到，`BuildJob`进入`failed`状态：

```bash
GET /api/v1/jenkins/builds/{build_id}
```

**响应 (200 OK)**:
```json
{
  "build_id": "a5d4eea7103f42f3a92b49c9e7f6be83",
  "status": "failed",
  "failure_site": "validating",
  "error": "Relative bind mount './data' is not supported for Git-based builds...",
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",
    "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",
    "summary": "...",
    "service": "redis",
    "config_path": "services.redis.volumes",
    "remediation": "...",
    "failure_site": "validating",
    "build_id": "a5d4eea7103f42f3a92b49c9e7f6be83",
    "recognized": true
  },
  "stage": "failed",
  "jenkins_queue_url": null,
  "jenkins_build_number": null
}
```

**关键观察点**:
- `failure_site == "validating"` 且 `error_detail.code == "COMPOSE_CONFIG_ERROR"` → 配置错误
- `failure_site == "jenkins"` → Jenkins构建失败（编译/测试错误）
- `jenkins_queue_url == null` 且 `jenkins_build_number == null` → 未触发Jenkins（早期失败）

## APPS集成指南

### 1. 错误分类决策树

```
if build.status == "failed":
    if build.error_detail and build.error_detail.get("code") == "COMPOSE_CONFIG_ERROR":
        → 这是Compose配置错误
        → 显示 error_detail.remediation 给用户
        → 不要提示"查看Jenkins日志"
    
    elif build.failure_site == "jenkins":
        → 这是Jenkins构建失败
        → 可以查看Jenkins控制台日志
    
    elif build.failure_site == "validating":
        → 验证阶段失败（可能是网络、认证等）
        → 检查 error_detail.exception_type 判断具体原因
    
    else:
        → 其他基础设施/部署错误
```

### 2. 向后兼容性

**旧版本字段仍然保留**:
- `error` (string): 用户可读的错误摘要
- `failure_site` (string): 失败阶段
- `stage` (string): 构建阶段（镜像`status`）
- `recognized` (boolean): ⚠️ **不要**用此字段判断是否为配置错误

**迁移建议**:
```python
# ❌ 旧方式（不准确）
if build.failure_site == "validating" and build.recognized:
    # 错误：所有错误都会设置recognized=True
    
# ✅ 新方式（准确）
if build.error_detail and build.error_detail.get("code") == "COMPOSE_CONFIG_ERROR":
    # 正确：明确判断配置错误
```

### 3. 部署注意事项

**字段可用性**:
- `error_detail.code`, `error_detail.subcode` 等新字段仅在**本次部署后**可用
- 部署前的构建记录不会有这些字段
- APPS应优雅降级处理缺失字段

**检查逻辑**:
```python
def is_compose_config_error(build):
    """判断是否为Compose配置错误（兼容新旧版本）"""
    error_detail = build.get("error_detail") or {}
    return error_detail.get("code") == "COMPOSE_CONFIG_ERROR"

def get_remediation(build):
    """获取修复建议（新版本）或空字符串（旧版本）"""
    error_detail = build.get("error_detail") or {}
    return error_detail.get("remediation", "")
```

### 4. UI显示建议

**配置错误UI**:
```
❌ 构建失败：Compose配置错误

服务: redis
问题: Git源不支持相对路径bind mount

💡 修复建议:
替换bind mount为Dockerfile COPY指令：
- 配置文件: COPY config/ /app/config/
- 持久化数据: 使用命名卷而非bind mount

详细信息: [展开error_detail.summary]
```

**非配置错误UI**:
```
❌ 构建失败：Jenkins构建错误

阶段: jenkins (构建)
Jenkins构建: #42

查看完整日志: [Jenkins控制台链接]
```

## 安全与脱敏

系统自动脱敏以下信息，**不会**出现在错误消息中：
- 环境变量值（如`SECRET_KEY=xxx`）
- 密钥内容
- 宿主机绝对路径（如`/srv/workspaces/...`）
- Harbor/Git认证凭据

**安全的错误消息示例**:
```
✅ "Relative bind mount './data' is not supported"
❌ "Path /srv/workspaces/app-one/snapshots/abc123/files/data does not exist"
```

## Compose最佳实践

### ✅ 推荐配置

#### Git源：使用命名卷 + Dockerfile COPY

```yaml
x-orchestrator-build-source: git  # 默认值

services:
  redis:
    image: redis:7.4-alpine
    volumes:
      - redis-data:/data  # ✅ 使用命名卷存储持久化数据

volumes:
  redis-data:  # ✅ 不设置 external: true，由构建托管
```

**Dockerfile** (配置文件):
```dockerfile
FROM redis:7.4-alpine
COPY redis.conf /usr/local/etc/redis/redis.conf  # ✅ 用COPY而非bind mount
CMD ["redis-server", "/usr/local/etc/redis/redis.conf"]
```

#### workspace源：可以使用bind mount

```yaml
x-orchestrator-build-source: workspace

services:
  app:
    build: .
    volumes:
      - ./config:/app/config:ro  # ✅ workspace源支持bind mount
```

上传工作区文件：
```bash
# 上传配置文件到workspace
POST /api/v1/workspace/APP-Demo
Content-Type: multipart/form-data

files: (config/app.json, {...})
```

### ❌ 不推荐配置

```yaml
# ❌ Git源使用相对bind mount（会失败）
x-orchestrator-build-source: git
services:
  app:
    volumes:
      - ./data:/app/data  # ❌ Git源不支持

# ❌ 使用宿主机绝对路径（安全隐患）
services:
  app:
    volumes:
      - /srv/data:/app/data  # ❌ 绕过托管机制

# ❌ 共享命名卷（跨构建污染）
volumes:
  shared-data:
    external: true  # ❌ 外部卷不受构建cleanup管理
    name: global-shared  # ❌ 多个构建共享，数据污染
```

## 卷生命周期管理

**重要**: 构建托管的命名卷会在cleanup时**自动删除**。

### 卷类型对比

| 卷类型 | 定义方式 | cleanup行为 | 适用场景 |
|--------|---------|-------------|----------|
| 构建托管卷 | `volumes: {vol-name: {}}` | ✅ 自动删除 | 临时数据、缓存 |
| 外部卷 | `volumes: {vol-name: {external: true}}` | ❌ 不删除 | ⚠️ 不推荐（污染风险） |
| Bind mount (workspace源) | `./path:/target` | N/A | 配置文件、源码 |

### 数据持久化建议

**构建隔离的数据**:
```yaml
volumes:
  redis-data:  # ✅ 构建独占，cleanup自动清理
```

**需要跨版本保留的数据**:
- ❌ **不要**使用Compose卷存储生产数据
- ✅ 使用外部数据库服务（RDS、云数据库）
- ✅ 使用对象存储（S3、OSS）
- ✅ 实施备份/迁移策略，而非依赖Docker卷

## 测试覆盖

以下场景已有完整测试覆盖：

- ✅ Git源使用bind mount触发`UNSUPPORTED_GIT_BIND_MOUNT`错误
- ✅ Workspace源bind mount验证文件存在性
- ✅ 命名卷对Git和workspace源均可用
- ✅ API同步422响应包含`error_detail`
- ✅ 异步构建失败包含结构化`error_detail`
- ✅ 错误消息脱敏（无密钥、绝对路径）
- ✅ 向后兼容旧客户端（保留`error`/`failure_site`字段）

## 回退计划

如果新版本有问题，回退时：

1. **数据库兼容**: 新字段（`error_detail.code`等）为可选，旧版本仍可读取
2. **API兼容**: 旧字段（`error`, `failure_site`）继续填充
3. **行为变更**: 回退后，Git源bind mount会**重新出现误导性错误**
   - 错误消息会变回 "upload it before creating the build"
   - 但不影响系统稳定性，仅UX下降

## FAQ

### Q: `recognized: true` 和 `code: "COMPOSE_CONFIG_ERROR"` 有什么区别？

A: 
- `recognized: true` 表示系统识别了错误（vs 未知异常），但**不代表是配置错误**
- 网络错误、认证失败等也会设置 `recognized: true`
- **只有** `code: "COMPOSE_CONFIG_ERROR"` 才明确表示Compose配置错误

### Q: Git源能否通过某种方式使用bind mount？

A: 不能。Git源的文件在Jenkins阶段通过`git clone`获取，validating阶段只有空的workspace snapshot。正确做法是：
- 配置文件 → Dockerfile `COPY`
- 运行时数据 → 命名Docker卷

### Q: 如何判断Jenkins是否被触发？

A: 检查 `jenkins_queue_url` 和 `jenkins_build_number`：
```python
if build.jenkins_queue_url is None and build.jenkins_build_number is None:
    # 未触发Jenkins（precheck或validating阶段失败）
else:
    # 已触发Jenkins
```

### Q: 部署后旧的构建记录会更新吗？

A: 不会。`error_detail.code`等新字段仅在**部署后新创建的构建**中出现。历史构建记录不会回填这些字段。

## 相关文档

- [Workspace API规范](./WORKSPACE_API.md) - Workspace上传/下载接口
- [构建流程](./BUILD_PIPELINE.md) - 完整的构建阶段说明
- [Jenkins集成](./JENKINS_INTEGRATION.md) - Jenkins触发和轮询机制

## 变更记录

- 2026-11-17: 初始版本，引入结构化错误分类系统
