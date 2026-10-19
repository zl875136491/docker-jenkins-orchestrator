# 2026-10-15 测试环境发布记录

状态：已上线并通过真实环境验收。所有时间为东八区（UTC+08:00）。

## 发布版本与入口

- 控制台：`http://10.32.12.110:18080/ui/`。
- API、Worker、Beat 统一镜像：`orchestrator-test:release-20261015-172006-build-isolation-final`。
- 镜像 ID：`sha256:ec1d8381f2d99e759b3689561a005d28fef0e566b6b48ae422ef02737efc2121`。
- Jenkins Job：`apps-orchestrator/orchestrator-real-delivery-20260920`。
- Pipeline SHA256：`739bef9b5ad6a6af3912e085676e89ee2e092be2507ed8ccf3ac7d35541ffc04`。
- 发布目录：`/opt/orchestrator-test/release-20261015-172006-build-isolation`，含冻结源码、证据、原配置、数据库及 workspace 备份。
- 原 `.env`、Mongo 数据库、JWT/Fernet 密钥及 Redis 数据保持不变；没有重新初始化测试环境。

已上线文件管理分页、上传/列表/删除、东八区时间输出、build 级资源隔离及按 `build_id` 查询/异步清理资源。

## 实际验证

- 完整回归：`749 passed`，包含可选真实浏览器测试。
- 最终镜像回归：`632 passed, 2 skipped`；两个跳过项是镜像没有安装可选 Playwright 的测试模块，不是业务失败。
- 完整 Pipeline 经真实 Jenkins Declarative linter 验证；更新后精确回读，并保留 sandbox 和其他 Job 设置；补齐 `BUILD_ID` 参数。
- 真实控制台登录、文件上传/删除、21 个文件的筛选与翻页、每页数量切换、构建资源查询通过，无 JavaScript 异常。
- 两轮真实 A/B 构建：Jenkins `#58`～`#61`。验证独立 Service、Container、Network、Volume、端口、只读 workspace 快照及镜像引用。
- 部分失败清理可重试；重复清理已完成构建返回 `200`，没有重复破坏性操作。

最终验收 APP：`RELEASE-ISOLATION-20261015-65301c6deb`。

| 构建 | build_id | 结果 |
| --- | --- | --- |
| A / Jenkins #60 | `d4d628cb04434f6cbddb0980ce6973ae` | 保留运行，入口 `http://10.32.12.110:18003` |
| B / Jenkins #61 | `73f262109bcb4fcea23c0bc6bd2b8f8c` | 构建成功；资源清理 `succeeded` |

A/B 使用相同 registry digest，引用和运行资源不同。清理 B 后 A 仍可访问，A 的镜像 tag/digest、卷、快照和运行资源不变；B 的私有 Service、Container、Volume、Network、端口预留、workspace 快照和镜像引用均已回收。APP、上传暂存区和 Build 历史保留。最终 APP 的后续构建源已固定到保留的 A 镜像 digest。

首轮验收 APP `RELEASE-ISOLATION-20261015-8c222de718` 的两个构建资源均已清理，保留历史和暂存输入，不占运行端口。

## 发布期间修复的实际问题

1. **Harbor 私有证书信任**：Docker 已信任的文件是服务端证书，不是系统根 CA。新增显式 `ORCHESTRATOR_HARBOR_CA_FILE`，只读挂载已有独立核验的信任锚。使用受控 partial-chain 验证，仍校验证书有效期和目标主机；没有关闭 TLS 验证。新增正确主机、错误主机、不可信及无效配置回归。
2. **workspace 管理挂载误判**：平台 API/Worker 的根目录管理挂载被当成业务占用。仅放行工作节点身份一致、角色标签一致且源/目标恰为 workspace 根目录的管理挂载。其他容器的父目录挂载和快照挂载仍阻止清理。
3. **Harbor 权限**：平台账号此前不是目标项目成员。只在 `apps-orchestrator` 项目（ID 9）增加该服务账号（用户 ID 13）的 Maintainer 成员权限，未授予系统管理员权限，未更换运行凭据。
4. **Harbor 会话 CSRF**：GET 生成的浏览器 session cookie 会使后续 Basic 认证删除请求被 CSRF 拒绝。清理适配器改为无状态 Basic 认证、不复用 cookie；Jenkins 的 crumb/session 行为保持原样。新增真实 HTTP 交互回归。

## 对 apps 的实际影响

- 16 个原有 Swarm Service 的 Spec 和 running task ID 均未改变；没有重启业务容器。
- 原有 29 个 APP、43 个 Build、24 个服务记录、29 个镜像记录和 26 条告警保持完整且内容未修改。
- 原有 59 个 workspace 文件校验和保持不变；`.env` 保持原校验和，旧 token 仍可访问；Redis 容器和启动时间未变。
- 单实例控制面经历三次短暂 API 不可用：17:28:20～17:28:44、17:48:12～17:48:30、18:00:11～18:00:29；采样合计约 59 秒。没有在活动构建中途切换。
- 后续新构建按 build 隔离、保留旧构建、不自动切流或复制旧卷数据；apps 应按 `build_id` 选取服务入口，不硬编码旧端口。
- 未使用 apps 的 APP 或历史构建做清理验收；没有执行全局 prune、重建 Swarm、全栈 down 或 Harbor 全局 GC。

## 存储回收边界

Harbor 的构建 tag 删除已验证；当 A/B 共用 digest 时返回 `shared_retained`，不会删除 A 使用的内容。没有其他 tag 时可能返回 `waiting_gc`，物理 blob 空间由 Harbor 后续 GC 回收。不能把 tag 删除视为已精确释放磁盘字节，因此 `reclaimed_bytes` 保持 `null`。Jenkins 资源报告验证其私有 workspace 和本地构建 tag 已移除。若将来失败构建在其他 Builder 留下资源且未配置其受控 Docker endpoint，清理会明确返回部分失败，不会伪报成功。

## 运维和回滚

现有环境运维请使用发布目录中的 `compose.sh`，或显式同时指定冻结源码 Compose 和 `release.override.yml`，不能忽略 Worker 的证书挂载配置：

```bash
/opt/orchestrator-test/current-release/compose.sh ps
```

回滚材料包含 `rollback.override.yml`、三个旧镜像 tag、`jenkins-config.before.xml`、`runtime.env.before`、`mongo.before/` 及 `workspaces.before.tar.gz`。回滚前暂停新构建、确认活动构建和清理均已结束；用 `restore_jenkins.py` 默认只校验，显式 `--apply` 才恢复授权测试 Job。配套切回旧平台镜像，不能只回滚 Pipeline 或 Worker 的一部分。默认不恢复整个数据库或 workspace 备份，避免覆盖 apps 在发布后新增的数据；新版本创建的独立业务资源也不应被全局删除。

证书轮换后需重新核验并替换挂载的信任文件。Harbor 项目成员权限变更记录见发布目录的 `harbor-project-permission-change.json`，如需撤销，按记录中的单个成员 ID 操作，不能修改其他项目或账号权限。
