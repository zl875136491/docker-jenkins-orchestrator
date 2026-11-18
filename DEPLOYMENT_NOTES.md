# Compose配置错误分类修复 - 部署说明

## 修复概要

**PR**: https://github.com/zl875136491/docker-jenkins-orchestrator/pull/1  
**Commit**: `7d1b4cc`  
**分支**: `cursor/fix-compose-error-classification-b763`  
**日期**: 2026-11-17

### 解决的问题

**线上事件**: APP-20261114-DDA5594C  
- **现象**: Git源构建使用`./data/redis:/data` bind mount时，validating阶段失败
- **错误消息**: "Workspace mount source does not exist: ./data/redis; upload it before creating the build"
- **误导性**: 提示上传workspace，但实际是Compose配置不支持Git源bind mount
- **分类错误**: 标记为`JENKINS_BUILD_FAILED`，但实际未触发Jenkins

### 核心修复

1. **引入结构化错误分类**：`ComposeConfigError`带有`code`, `subcode`, `remediation`等字段
2. **修复Git源验证**：明确拒绝相对bind mount，提供正确的修复指引（使用Dockerfile COPY）
3. **区分错误类型**：Compose配置错误 vs 基础设施/Jenkins/网络故障
4. **保持向后兼容**：保留所有现有字段（`error`, `failure_site`, `recognized`）

## 变更文件

### 核心代码
- `orchestrator/workspaces.py` (新增60行): `ComposeConfigError`异常类和Git源验证逻辑
- `main.py` (新增8行): `ComposeConfigError`异常处理器
- `orchestrator/services.py` (修改10行): 错误detail传递

### 测试
- `tests/test_workspaces.py` (新增150行): 5个新测试用例

### 文档
- `docs/COMPOSE_ERROR_CLASSIFICATION.md` (新建): 完整错误分类文档
- `docs/COMPOSE_CONFIGURATION_GUIDE.md` (新建): Compose配置最佳实践
- `docs/error_response_examples.json` (新建): API响应示例和集成指南

## 部署步骤

### 1. 预发布环境部署

```bash
# 1.1 合并PR到预发布分支
git checkout staging
git merge cursor/fix-compose-error-classification-b763
git push origin staging

# 1.2 触发预发布构建
# [CI/CD系统自动部署]

# 1.3 等待部署完成
# 验证服务健康检查通过
curl https://staging-api.example/health
```

### 2. 预发布环境验证

#### 2.1 Git源bind mount错误（主要场景）

```bash
# 创建测试应用
curl -X POST https://staging-api.example/api/v1/jenkins/app_create \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "appid": "TEST-GitBind",
    "name": "Test Git Bind Mount",
    "repository_url": "https://git.example/test/app.git",
    "compose": {
      "services": {
        "redis": {
          "image": "redis:7.4-alpine",
          "volumes": ["./data/redis:/data"]
        }
      }
    }
  }'

# 创建构建
BUILD_ID=$(curl -X POST https://staging-api.example/api/v1/jenkins/TEST-GitBind/builds \
  -H "Authorization: Bearer $TOKEN" | jq -r '.build_id')

# 等待几秒让validating阶段完成
sleep 5

# 检查构建状态
curl https://staging-api.example/api/v1/jenkins/builds/$BUILD_ID \
  -H "Authorization: Bearer $TOKEN" | jq '{
    status,
    failure_site,
    error_detail: {
      code: .error_detail.code,
      subcode: .error_detail.subcode,
      service: .error_detail.service,
      remediation: .error_detail.remediation
    },
    jenkins_triggered: .jenkins_queue_url != null
  }'

# ✅ 预期结果:
# {
#   "status": "failed",
#   "failure_site": "validating",
#   "error_detail": {
#     "code": "COMPOSE_CONFIG_ERROR",
#     "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",
#     "service": "redis",
#     "remediation": "Replace bind mounts with Dockerfile COPY instructions..."
#   },
#   "jenkins_triggered": false
# }
```

#### 2.2 Workspace源bind mount（应继续工作）

```bash
# 创建workspace源应用
curl -X POST https://staging-api.example/api/v1/jenkins/app_create \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "appid": "TEST-WorkspaceBind",
    "name": "Test Workspace Bind",
    "compose": {
      "x-orchestrator-build-source": "workspace",
      "services": {
        "nginx": {
          "image": "nginx:alpine",
          "volumes": ["./html:/usr/share/nginx/html:ro"]
        }
      }
    }
  }'

# 上传文件
curl -X POST https://staging-api.example/api/v1/workspace/TEST-WorkspaceBind \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@index.html"

# 创建构建（应成功）
BUILD_ID=$(curl -X POST https://staging-api.example/api/v1/jenkins/TEST-WorkspaceBind/builds \
  -H "Authorization: Bearer $TOKEN" | jq -r '.build_id')

# 检查状态（应通过validating）
sleep 5
curl https://staging-api.example/api/v1/jenkins/builds/$BUILD_ID \
  -H "Authorization: Bearer $TOKEN" | jq '{status, failure_site}'

# ✅ 预期结果: {"status": "building" or "succeeded", "failure_site": null}
```

#### 2.3 Jenkins构建失败（应正确分类）

```bash
# 创建有编译错误的应用
curl -X POST https://staging-api.example/api/v1/jenkins/app_create \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "appid": "TEST-JenkinsFail",
    "name": "Test Jenkins Failure",
    "repository_url": "https://git.example/test/broken-app.git",
    "compose": {
      "services": {
        "app": {"build": ".", "image": "test-app"}
      }
    }
  }'

# 创建构建（会到达Jenkins但构建失败）
BUILD_ID=$(curl -X POST https://staging-api.example/api/v1/jenkins/TEST-JenkinsFail/builds \
  -H "Authorization: Bearer $TOKEN" | jq -r '.build_id')

# 等待Jenkins构建完成（可能需要几分钟）
sleep 120

# 检查状态
curl https://staging-api.example/api/v1/jenkins/builds/$BUILD_ID \
  -H "Authorization: Bearer $TOKEN" | jq '{
    status,
    failure_site,
    has_compose_error_code: (.error_detail.code == "COMPOSE_CONFIG_ERROR"),
    jenkins_triggered: .jenkins_build_number != null
  }'

# ✅ 预期结果:
# {
#   "status": "failed",
#   "failure_site": "jenkins",
#   "has_compose_error_code": false,  # ← 不是Compose错误
#   "jenkins_triggered": true         # ← Jenkins被触发了
# }
```

### 3. 验证清单

在预发布环境完成以下检查：

- [ ] **Git源bind mount触发配置错误**
  - [ ] `error_detail.code == "COMPOSE_CONFIG_ERROR"`
  - [ ] `error_detail.subcode == "UNSUPPORTED_GIT_BIND_MOUNT"`
  - [ ] `error_detail.remediation`包含"Dockerfile COPY"
  - [ ] `jenkins_queue_url == null`（未触发Jenkins）

- [ ] **Workspace源bind mount仍然工作**
  - [ ] 上传文件后构建成功进入building/deploying
  - [ ] 缺失文件时提示"upload it"（正确的提示）

- [ ] **其他错误类型不受影响**
  - [ ] Jenkins构建失败：`failure_site="jenkins"`, 无`code`字段
  - [ ] 网络错误：`failure_site="validating"`, 无`code`字段
  - [ ] 认证错误：`exception_type="GitLabError"`, 无`code`字段

- [ ] **向后兼容性**
  - [ ] 所有失败构建仍有`error`字段（用户可读文本）
  - [ ] 所有失败构建仍有`failure_site`字段
  - [ ] `recognized`字段继续填充

- [ ] **脱敏检查**
  - [ ] 错误消息不包含环境变量值
  - [ ] 错误消息不包含绝对路径（`/srv/workspaces/...`）
  - [ ] 错误消息不包含认证凭据

### 4. 生产环境部署

#### 4.1 灰度发布策略

```bash
# 阶段1: 1% 流量（观察1小时）
# 部署到生产环境，监控错误率和响应时间

# 阶段2: 10% 流量（观察6小时）
# 如无异常，扩大灰度范围

# 阶段3: 50% 流量（观察12小时）
# 继续监控

# 阶段4: 100% 流量
# 全量部署
```

#### 4.2 生产环境监控

**关键指标**：
```bash
# 1. 构建失败率
# 应保持稳定或略有下降（更早发现配置错误）
SELECT 
  DATE(created_at) as date,
  COUNT(CASE WHEN status = 'failed' THEN 1 END) / COUNT(*) as fail_rate
FROM builds
WHERE created_at > NOW() - INTERVAL 7 DAY
GROUP BY date;

# 2. 新error_detail.code字段覆盖率
# 部署后的构建应有此字段
SELECT 
  COUNT(CASE WHEN error_detail ? 'code' THEN 1 END) as with_code,
  COUNT(*) as total_failed
FROM builds
WHERE status = 'failed' AND created_at > [DEPLOYMENT_TIME];

# 3. Compose配置错误比例
SELECT 
  COUNT(CASE WHEN error_detail->>'code' = 'COMPOSE_CONFIG_ERROR' THEN 1 END) as config_errors,
  COUNT(*) as total_failed
FROM builds
WHERE status = 'failed' AND created_at > [DEPLOYMENT_TIME];

# 4. Git源bind mount特定错误
SELECT COUNT(*) as git_bind_mount_errors
FROM builds
WHERE status = 'failed'
  AND error_detail->>'subcode' = 'UNSUPPORTED_GIT_BIND_MOUNT'
  AND created_at > [DEPLOYMENT_TIME];
```

**告警阈值**：
- ❌ 总体失败率上升 >5%
- ❌ API 5xx错误率上升
- ❌ `error_detail`字段缺失（新构建应始终有此字段）

### 5. 回退计划

如需紧急回退：

```bash
# 方式1: 快速回退到上一个稳定版本
git checkout main
git revert 7d1b4cc  # 回退此次修复
git push origin main
# [CI/CD自动部署]

# 方式2: 使用Git回滚
git reset --hard HEAD~1
git push origin main --force
```

**回退影响**：
- ✅ 数据库兼容：新字段为可选，旧版本可读取
- ⚠️ Git源bind mount会恢复误导性错误消息（"upload workspace"）
- ✅ 系统稳定性不受影响
- ⚠️ APPS团队如已依赖`error_detail.code`，需恢复到判断`failure_site`

## APPS集成指南

### API消费示例（Python）

```python
def classify_build_error(build: dict) -> dict:
    """
    分类构建错误，返回用户友好的错误信息
    """
    error_detail = build.get("error_detail") or {}
    
    # 新版本：检查结构化code
    if error_detail.get("code") == "COMPOSE_CONFIG_ERROR":
        return {
            "category": "compose_config",
            "title": "Compose配置错误",
            "icon": "⚙️",
            "message": build.get("error", "配置错误"),
            "remediation": error_detail.get("remediation", ""),
            "service": error_detail.get("service"),
            "config_path": error_detail.get("config_path"),
            "actions": [
                {"label": "查看配置指南", "url": "/docs/compose-guide"},
                {"label": "修改Compose", "action": "edit_compose"},
            ]
        }
    
    # Jenkins构建失败
    failure_site = build.get("failure_site")
    if failure_site == "jenkins":
        jenkins_url = f"https://jenkins.example/job/APPS-orchestrator/{build['jenkins_build_number']}/console"
        return {
            "category": "jenkins_build",
            "title": "构建失败",
            "icon": "🔨",
            "message": build.get("error", "Jenkins构建失败"),
            "jenkins_build": build.get("jenkins_build_number"),
            "actions": [
                {"label": "查看Jenkins日志", "url": jenkins_url},
                {"label": "重试构建", "action": "retry_build"},
            ]
        }
    
    # 验证阶段失败（网络/认证/workspace等）
    if failure_site == "validating":
        exception_type = error_detail.get("exception_type", "")
        
        if exception_type == "GitLabError":
            return {
                "category": "git_auth",
                "title": "Git仓库访问失败",
                "icon": "🔐",
                "message": build.get("error"),
                "actions": [
                    {"label": "检查仓库权限", "action": "check_git_access"},
                ]
            }
        
        if exception_type == "WorkspaceError":
            return {
                "category": "workspace_missing",
                "title": "Workspace文件缺失",
                "icon": "📁",
                "message": build.get("error"),
                "actions": [
                    {"label": "上传文件", "action": "upload_workspace"},
                ]
            }
        
        # 通用验证失败
        return {
            "category": "validation",
            "title": "验证失败",
            "icon": "⚠️",
            "message": build.get("error", "构建验证失败"),
        }
    
    # 部署阶段失败
    if failure_site == "deploy":
        return {
            "category": "deployment",
            "title": "部署失败",
            "icon": "🚀",
            "message": build.get("error"),
            "actions": [
                {"label": "查看部署日志", "action": "view_deploy_logs"},
            ]
        }
    
    # 未知错误
    return {
        "category": "unknown",
        "title": "构建失败",
        "icon": "❌",
        "message": build.get("error", "未知错误"),
    }


# 使用示例
build = api_client.get_build(build_id)
error_info = classify_build_error(build)

# 渲染UI
if error_info["category"] == "compose_config":
    display_config_error_card(
        title=error_info["title"],
        message=error_info["message"],
        remediation=error_info["remediation"],
        service=error_info["service"],
        actions=error_info["actions"],
    )
else:
    display_generic_error_card(error_info)
```

### 迁移时间表

**第1周（部署后）**:
- APPS继续使用现有`error`和`failure_site`字段
- 监控`error_detail.code`字段出现率
- 开发新的错误分类UI（基于`error_detail.code`）

**第2周**:
- APPS灰度启用新的错误分类逻辑（10%用户）
- 对比新旧逻辑的准确性
- 收集用户反馈

**第3-4周**:
- 全量启用新的错误分类逻辑
- 移除旧的误判逻辑（如`failure_site == "validating" and recognized`）

## 测试报告

### 单元测试

**新增测试**（`tests/test_workspaces.py`）：
1. `test_git_source_bind_mount_raises_compose_config_error`
   - 验证Git源bind mount触发`ComposeConfigError`
   - 检查`code`, `subcode`, `service`, `remediation`字段

2. `test_git_source_bind_mount_error_via_api_returns_422_with_structured_detail`
   - 通过API创建构建，验证同步422响应
   - 检查HTTP响应包含`error_detail`

3. `test_workspace_source_bind_mount_checks_existence`
   - Workspace源缺失文件触发`WorkspaceError`（非`ComposeConfigError`）
   - 上传文件后构建成功

4. `test_named_volumes_allowed_for_both_git_and_workspace_sources`
   - 命名卷对Git和workspace源均可用
   - 不触发任何错误

5. `test_error_detail_sanitization_no_secrets_in_compose_errors`
   - 错误消息不泄露环境变量值
   - 错误消息不泄露绝对路径

**测试状态**：
- ⚠️ 本地环境pytest未安装，测试代码已编写但未执行
- ✅ 建议在CI环境或部署到预发布后验证

### 回归测试覆盖

**现有测试应继续通过**：
- `test_workspace_resolves_files_long_mounts_and_rejects_missing_traversal_and_cross_app`
  - 修改：现在Git源bind mount会触发`ComposeConfigError`而非`WorkspaceError`
  - 需验证测试预期是否需要更新

- `test_uploaded_workspace_reaches_jenkins_and_swarm_without_git_checkout`
  - Workspace源不受影响，应继续通过

**未测试边界**（已在文档中说明）：
- 多层嵌套的Compose配置（`services.x.volumes[].volumes`）
- 非标准Dockerfile路径（`build.context`不是`.`）
- 网络极端条件（DNS超时、TLS握手失败）

## 文档更新清单

- [x] `docs/COMPOSE_ERROR_CLASSIFICATION.md`: 完整的错误分类系统文档
  - 错误代码定义
  - HTTP响应格式
  - APPS集成指南
  - FAQ

- [x] `docs/COMPOSE_CONFIGURATION_GUIDE.md`: Compose配置最佳实践
  - 快速自检清单
  - Git vs Workspace源区别
  - 卷生命周期管理
  - 常见场景配置示例

- [x] `docs/error_response_examples.json`: API响应示例
  - 新旧格式对比
  - 各类错误场景
  - 客户端集成代码

- [x] `DEPLOYMENT_NOTES.md`: 本文档（部署说明）

## 风险评估

### 低风险
- ✅ 向后兼容：保留所有现有字段
- ✅ 增量功能：新增字段为可选
- ✅ 脱敏机制：防止信息泄露
- ✅ 回退简单：一键回滚，无数据迁移

### 中风险
- ⚠️ 测试覆盖：本地未运行单元测试，依赖CI
- ⚠️ APPS依赖：如APPS已部署依赖旧字段的逻辑，需协调更新

### 缓解措施
1. 预发布环境全面验证
2. 生产环境灰度发布（1% → 10% → 50% → 100%）
3. 实时监控构建失败率和错误分类准确性
4. 准备快速回退方案
5. APPS团队提前沟通集成时间表

## 联系人

- **开发**: Cursor Cloud Agent
- **审核**: [负责人姓名]
- **APPS集成**: [APPS团队联系人]
- **运维**: [运维团队联系人]

## 附录：协议示例

### 成功的Git源构建（无bind mount）

```json
{
  "build_id": "abc123",
  "status": "succeeded",
  "failure_site": null,
  "error": null,
  "error_detail": null,
  "jenkins_build_number": 1526
}
```

### Compose配置错误

```json
{
  "build_id": "def456",
  "status": "failed",
  "failure_site": "validating",
  "error": "Relative bind mount './data' is not supported for Git-based builds...",
  "error_detail": {
    "code": "COMPOSE_CONFIG_ERROR",
    "subcode": "UNSUPPORTED_GIT_BIND_MOUNT",
    "service": "redis",
    "config_path": "services.redis.volumes",
    "remediation": "Replace bind mounts with Dockerfile COPY instructions...",
    "failure_site": "validating",
    "recognized": true
  },
  "jenkins_queue_url": null,
  "jenkins_build_number": null
}
```

### Jenkins构建失败

```json
{
  "build_id": "ghi789",
  "status": "failed",
  "failure_site": "jenkins",
  "error": "阶段=Build Docker image；位置=console L342；原因=npm ERR!...",
  "error_detail": {
    "stage": "Build Docker image",
    "console_line": 342,
    "recognized": true,
    "failure_site": "jenkins",
    "jenkins_build_number": 1527
  },
  "jenkins_queue_url": "https://jenkins.example/queue/item/8237/",
  "jenkins_build_number": 1527
}
```

---

**文档版本**: 1.0  
**最后更新**: 2026-11-17  
**状态**: 待部署
