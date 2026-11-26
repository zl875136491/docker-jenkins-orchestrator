# Compose配置错误分类 - APPS集成文档

**版本**: 部署候选版本  
**Commit**: `c98658a`  
**日期**: 2026-11-18  
**PR**: https://github.com/zl875136491/docker-jenkins-orchestrator/pull/1

---

## 一、变更概要

### 解决的问题

**线上事件 APP-20261114-DDA5594C**：Git源构建使用bind mount时，validating阶段失败并误报"upload workspace"，实际应提示配置不支持。

### 核心改动

1. **新增结构化错误分类**：引入`error_detail.code`字段区分Compose配置错误与基础设施故障
2. **修复Git源验证逻辑**：明确拒绝相对路径bind mount并提供修复建议
3. **保持向后兼容**：保留所有现有字段（`error`, `failure_site`, `recognized`）

---

## 二、新增字段

### BuildJob新增字段（可选）

仅部署后**新创建的失败构建**包含以下字段，历史记录不回填。

```json
{
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",           // 新增：错误代码（仅Compose配置错误有此字段）
    "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",  // 新增：具体子类型
    "service": "redis",                       // 新增：出错的服务名
    "config_path": "services.redis.volumes",  // 新增：出错的配置路径
    "remediation": "Replace bind mounts...",  // 新增：修复建议
    "failure_site": "validating",             // 已有：失败阶段
    "recognized": true                         // 已有：是否已识别
  }
}
```

### 当前支持的错误代码

| code | subcode | 说明 | 触发时机 |
|------|---------|------|---------|
| `COMPOSE_CONFIG_ERROR` | `UNSUPPORTED_GIT_BIND_MOUNT` | Git源使用相对路径bind mount | validating阶段 |
| `COMPOSE_CONFIG_ERROR` | `BUILD_CONTEXT_MISSING` | `build.context` 目录不存在 | validating / Jenkins Validate Compose |
| `COMPOSE_CONFIG_ERROR` | `DOCKERFILE_MISSING` | Dockerfile 不存在 | validating / Jenkins Validate Compose |
| `JENKINS_BUILD_ERROR` | 见 [APPS_ERROR_CONTRACT.md](APPS_ERROR_CONTRACT.md) | Jenkins 构建期分类错误 | jenkins 阶段 |

完整字段优先级、`raw_log_excerpt` 约定与全部 subcode 表见 **[APPS_ERROR_CONTRACT.md](APPS_ERROR_CONTRACT.md)**。

---

## 三、受影响接口

### 1. 同步创建构建（立即422）

**可能但不一定触发**：如果创建时已能检测到配置错误，立即返回422。

```bash
POST /api/v1/jenkins/{appid}/builds
```

**响应示例 (HTTP 422)**：
```json
{
  "detail": "Relative bind mount './data' is not supported for Git-based builds...",
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",
    "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",
    "service": "redis",
    "config_path": "services.redis.volumes",
    "remediation": "Replace bind mounts with Dockerfile COPY instructions...",
    "recognized": true
  }
}
```

### 2. 异步构建失败（轮询结果）

**正常触发方式**：大多数配置错误在异步validating阶段检测到。

```bash
GET /api/v1/jenkins/builds/{build_id}
```

**响应示例 (HTTP 200, status=failed)**：
```json
{
  "build_id": "a5d4eea7103f42f3a92b49c9e7f6be83",
  "status": "failed",
  "failure_site": "validating",
  "error": "Relative bind mount './data/redis' is not supported...",
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",
    "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",
    "service": "redis",
    "config_path": "services.redis.volumes",
    "remediation": "Replace bind mounts with Dockerfile COPY instructions...",
    "failure_site": "validating",
    "build_id": "a5d4eea7103f42f3a92b49c9e7f6be83",
    "recognized": true
  },
  "jenkins_queue_url": null,
  "jenkins_build_number": null,
  "stage": "failed"
}
```

---

## 四、客户端分类逻辑

### 推荐判断流程

```python
def is_compose_config_error(build: dict) -> bool:
    """判断是否为Compose配置错误（新版本）"""
    error_detail = build.get("error_detail")
    if not error_detail:
        return False
    return error_detail.get("code") == "COMPOSE_CONFIG_ERROR"


def classify_error(build: dict) -> str:
    """分类构建错误"""
    
    # 1. Compose配置错误（本次新增）
    if is_compose_config_error(build):
        return "compose_config_error"
    
    # 2. Jenkins构建失败（已有）
    if build.get("failure_site") == "jenkins":
        return "jenkins_build_failure"
    
    # 3. validating阶段其他错误（网络/认证/workspace等）
    if build.get("failure_site") == "validating":
        exception_type = build.get("error_detail", {}).get("exception_type")
        if exception_type == "GitLabError":
            return "git_auth_failure"
        elif exception_type == "WorkspaceError":
            return "workspace_file_missing"
        return "validation_failure"
    
    # 4. 部署失败
    if build.get("failure_site") == "deploy":
        return "deployment_failure"
    
    return "unknown_failure"
```

### 兜底逻辑

```python
def get_user_message(build: dict) -> str:
    """获取用户可读错误消息（兜底）"""
    
    # 优先使用error字段（向后兼容）
    error_msg = build.get("error")
    if error_msg:
        return error_msg
    
    # 兜底：从error_detail提取
    error_detail = build.get("error_detail", {})
    raw = build.get("raw_log_excerpt") or (build.get("failure_logs") or {}).get("raw_log_excerpt")
    if raw:
        return error_detail.get("summary") or "构建失败，详情见 raw_log_excerpt"
    return error_detail.get("summary") or "构建失败，详情暂不可用（无 raw_log_excerpt）"


def get_remediation(build: dict) -> str:
    """获取修复建议（新版本功能）"""
    error_detail = build.get("error_detail", {})
    return error_detail.get("remediation", "")
```

---

## 五、原始字段兼容性

### 保留字段（向后兼容）

所有失败构建**继续填充**以下字段，旧客户端可继续使用：

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `error` | string | 用户可读错误摘要 | "Relative bind mount './data' is not supported..." |
| `failure_site` | string | 失败阶段 | "validating", "jenkins", "deploy" |
| `stage` | string | 构建阶段（镜像status） | "failed" |
| `recognized` | boolean | 是否已识别 | true（所有错误都是true，**不代表配置错误**） |

### ⚠️ 重要：recognized不等于配置错误

```python
# ❌ 错误判断（不准确）
if build["failure_site"] == "validating" and build["recognized"]:
    # 所有错误recognized都是true，包括网络/认证等非配置错误
    show_config_error_ui(build)

# ✅ 正确判断（准确）
if build.get("error_detail", {}).get("code") == "COMPOSE_CONFIG_ERROR":
    show_config_error_ui(build)
else:
    show_generic_error_ui(build)
```

---

## 六、历史记录与旧版本

### 部署前构建记录

- **不回填**：历史失败构建没有`error_detail.code`字段
- **兼容处理**：客户端应优雅处理缺失字段

```python
def safe_get_code(build: dict) -> str | None:
    """安全获取错误代码（兼容旧记录）"""
    error_detail = build.get("error_detail")
    if not error_detail:
        return None
    return error_detail.get("code")

# 使用示例
code = safe_get_code(build)
if code == "COMPOSE_CONFIG_ERROR":
    # 明确是配置错误
    pass
elif code is None:
    # 旧记录或非配置错误，使用fallback逻辑
    pass
```

### 降级兼容

部署前后API响应都包含`error`和`failure_site`字段：

```python
# 兼容新旧两种部署状态
def display_error(build: dict):
    # 新版本：尝试使用code分类
    if is_compose_config_error(build):
        show_config_error_card(build)
        return
    
    # 旧版本或非配置错误：使用failure_site
    failure_site = build.get("failure_site")
    if failure_site == "jenkins":
        show_jenkins_error_card(build)
    elif failure_site == "validating":
        show_validation_error_card(build)
    else:
        show_generic_error_card(build)
```

---

## 七、卷生命周期与数据警告

### 构建托管的命名卷

**清理行为**：执行构建cleanup时**自动删除**构建托管的卷及其数据。

```yaml
# 示例Compose配置
volumes:
  redis-data:  # ← 构建托管卷
```

**清理时删除**：
```bash
DELETE /api/v1/jenkins/builds/{build_id}
# → 删除所有容器、网络、卷（包括redis-data）
```

### ⚠️ 跨构建数据不自动保留

**场景**：从build v1升级到build v2

```
build v1: redis-data → /var/lib/docker/volumes/build-abc123-redis-data
build v2: redis-data → /var/lib/docker/volumes/build-def456-redis-data
                        ↑ 新卷，旧数据不自动迁移
```

**结果**：v2启动时是空卷，v1数据不会自动迁移。

### 数据持久化建议

| 数据类型 | 推荐方案 | 不推荐 |
|---------|---------|--------|
| 临时缓存 | 构建托管卷 | ✅ |
| 运行时日志 | 构建托管卷 | ✅ |
| 生产数据 | 外部数据库服务（RDS等） | ✅ |
| 跨版本保留数据 | 手动备份/恢复 + 外部服务 | ⚠️ |
| | Docker卷 + 依赖cleanup不触发 | ❌ |

---

## 八、验收清单

部署到预发布环境后，执行以下验证：

### 8.1 Git源bind mount触发配置错误

```bash
# 创建测试应用（Git源 + bind mount）
curl -X POST https://staging-api/api/v1/jenkins/app_create \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "appid": "TEST-GitBind",
    "name": "Test Git Bind",
    "repository_url": "https://git.example/test.git",
    "compose": {
      "services": {
        "redis": {
          "image": "redis:7",
          "volumes": ["./data/redis:/data"]
        }
      }
    }
  }'

# 创建构建并检查失败
BUILD_ID=$(curl -X POST https://staging-api/api/v1/jenkins/TEST-GitBind/builds \
  -H "Authorization: Bearer $TOKEN" | jq -r '.build_id')

sleep 5

BUILD=$(curl https://staging-api/api/v1/jenkins/builds/$BUILD_ID \
  -H "Authorization: Bearer $TOKEN")

# ✅ 验证点
echo $BUILD | jq '{
  status,                                    # 应为 "failed"
  failure_site,                              # 应为 "validating"
  has_code: (.error_detail.code != null),    # 应为 true
  code: .error_detail.code,                  # 应为 "COMPOSE_CONFIG_ERROR"
  subcode: .error_detail.subcode,            # 应为 "UNSUPPORTED_GIT_BIND_MOUNT"
  service: .error_detail.service,            # 应为 "redis"
  has_remediation: (.error_detail.remediation != null), # 应为 true
  jenkins_triggered: (.jenkins_queue_url != null)       # 应为 false
}'
```

**预期输出**：
```json
{
  "status": "failed",
  "failure_site": "validating",
  "has_code": true,
  "code": "COMPOSE_CONFIG_ERROR",
  "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",
  "service": "redis",
  "has_remediation": true,
  "jenkins_triggered": false
}
```

### 8.2 Workspace源bind mount继续工作

```bash
# 创建workspace源应用
curl -X POST https://staging-api/api/v1/jenkins/app_create \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "appid": "TEST-Workspace",
    "name": "Test Workspace",
    "compose": {
      "x-orchestrator-build-source": "workspace",
      "services": {
        "nginx": {
          "image": "nginx",
          "volumes": ["./html:/usr/share/nginx/html:ro"]
        }
      }
    }
  }'

# 上传文件
curl -X POST https://staging-api/api/v1/workspace/TEST-Workspace \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@index.html"

# 创建构建（应成功）
BUILD_ID=$(curl -X POST https://staging-api/api/v1/jenkins/TEST-Workspace/builds \
  -H "Authorization: Bearer $TOKEN" | jq -r '.build_id')

sleep 10

# ✅ 验证点：应进入building或succeeded状态
curl https://staging-api/api/v1/jenkins/builds/$BUILD_ID \
  -H "Authorization: Bearer $TOKEN" | jq '{status, failure_site}'

# 预期: {"status": "building" or "succeeded", "failure_site": null}
```

### 8.3 Jenkins构建失败不误判

创建有编译错误的应用，验证`failure_site == "jenkins"`且无`code`字段。

### 8.4 向后兼容性

```bash
# 验证旧字段仍存在
BUILD=$(curl https://staging-api/api/v1/jenkins/builds/$FAILED_BUILD_ID \
  -H "Authorization: Bearer $TOKEN")

echo $BUILD | jq '{
  has_error: (.error != null),         # ✅ 应为 true
  has_failure_site: (.failure_site != null), # ✅ 应为 true
  has_recognized: (.recognized != null)     # ✅ 应为 true
}'
```

---

## 九、部署与回退

### 部署要求

- 协调端负责实际部署，前端不访问正式机
- 部署后新构建才包含`error_detail.code`字段
- 历史构建不回填

### 回退影响

如需回退到上一版本：

| 影响项 | 状态 |
|--------|------|
| 数据库兼容性 | ✅ 新字段可选，旧版本可读 |
| API兼容性 | ✅ 旧字段继续填充 |
| Git源bind mount错误消息 | ⚠️ 恢复误导性提示（"upload workspace"） |
| APPS依赖新字段 | ⚠️ 需恢复到failure_site判断 |

### 监控指标

部署后监控以下指标：

```sql
-- 1. 新字段覆盖率（部署后应接近100%）
SELECT 
  COUNT(CASE WHEN error_detail ? 'code' THEN 1 END) as with_code,
  COUNT(*) as total_failed
FROM builds
WHERE status = 'failed' 
  AND created_at > [部署时间];

-- 2. Compose配置错误数量
SELECT COUNT(*) as config_errors
FROM builds
WHERE status = 'failed'
  AND error_detail->>'code' = 'COMPOSE_CONFIG_ERROR'
  AND created_at > [部署时间];

-- 3. Git源bind mount特定错误
SELECT COUNT(*) as git_bind_errors
FROM builds
WHERE status = 'failed'
  AND error_detail->>'subcode' = 'UNSUPPORTED_GIT_BIND_MOUNT'
  AND created_at > [部署时间];
```

---

## 十、技术支持

**PR链接**: https://github.com/zl875136491/docker-jenkins-orchestrator/pull/1  
**测试状态**: 34/34 passed  
**文档版本**: 1.0 (2026-11-18)

**联系方式**：[内部技术支持渠道]
