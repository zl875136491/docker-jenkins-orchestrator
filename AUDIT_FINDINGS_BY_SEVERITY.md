# 项目缺陷与功能覆盖审计摘要

## 1. 审计基准

- 审计日期：2026 年 10 月 12 日
- 审计基准提交：`d48b66332558030b97902574e8ff53303e80b1bb`
- 审计基准分支：`main`
- 审计基准工作区：干净
- 本地测试结果：`222 passed, 1 warning`
- 语法检查：Python `compileall` 与 `node --check webui/app.js` 通过
- 测试环境存活检查：`GET /healthz` 返回 `{"status":"ok"}`
- 覆盖率：无法生成。环境未安装 `pytest-cov/coverage`，项目也未发现覆盖率门禁、CI、lint 或类型检查配置。

> 注意：生成本文档时工作区已经存在其他未提交修改。本文档整理的是上一次审计基准的结果，不代表这些未提交修改已经修复了下列问题；如需确认修复状态，应在修改合并后重新执行针对性回归测试。

## 2. 严重性定义

| 等级 | 定义 | 处理建议 |
| --- | --- | --- |
| P0 | 立即造成大范围数据泄露、不可恢复数据损坏或核心系统完全不可用 | 立即停止生产发布，先修复并验证 |
| P1 | 可能造成跨应用数据污染、构建重复执行、服务中断、状态损坏或敏感数据泄露 | 生产发布前必须修复或设置明确的运行隔离 |
| P2 | 影响单个流程的正确性、可恢复性、运维效率或用户体验，但通常有规避方式 | 纳入近期迭代并补回归测试 |
| P3 | 安全加固、工程质量、功能完整性或测试覆盖不足 | 持续改进，发布前完成风险评估 |

本轮没有确认到 P0；但 P1 问题较多，当前系统不宜在没有额外隔离和人工运维措施的情况下直接承载高并发生产任务。

## 3. 按严重性汇总

### P1：生产发布前必须处理

| 编号 | 问题 | 主要影响 | 证据 |
| --- | --- | --- | --- |
| P1-01 | 构建触发与部署没有原子领取/租约 | 同一 build 可能重复触发 Jenkins、重复轮询、重复部署和重复写入成功事件 | [`worker_runtime.py:145`](orchestrator/worker_runtime.py#L145)、[`worker_runtime.py:276`](orchestrator/worker_runtime.py#L276)、[`worker_runtime.py:319`](orchestrator/worker_runtime.py#L319) |
| P1-02 | Swarm 原地更新失败不能恢复旧服务 | 新版本未 ready 时旧版本已经被替换，应用可能中断 | [`docker_services.py:675`](orchestrator/docker_services.py#L675)、[`docker_services.py:419`](orchestrator/docker_services.py#L419) |
| P1-03 | 普通 Docker volume 没有按 app 隔离 | 不同 app 使用相同 alias 时可能读写同一个数据卷，造成跨应用数据泄露或覆盖 | [`docker_services.py:576`](orchestrator/docker_services.py#L576)、[`docker_services.py:1208`](orchestrator/docker_services.py#L1208) |
| P1-04 | Jenkins artifact 丢失顶层 `volumes` 元数据 | `name`、`external` 等信息丢失后可能挂载错误卷或空卷 | [`orchestrator-build.groovy:180`](jenkins/orchestrator-build.groovy#L180)、[`worker_runtime.py:476`](orchestrator/worker_runtime.py#L476) |
| P1-05 | Compose 中的秘密可能明文流转和持久化 | 密码、token、secret 可能出现在 API、Mongo、UI、Jenkins 参数和 artifact 中 | [`models.py:126`](orchestrator/models.py#L126)、[`jenkins.py:75`](orchestrator/jenkins.py#L75)、[`orchestrator-build.groovy:217`](jenkins/orchestrator-build.groovy#L217) |
| P1-06 | `app_sync` 与 PATCH 存在 TOCTOU 覆盖竞态 | sync 可能用旧分支快照覆盖用户的新分支或手工 Compose，形成混合状态 | [`services.py:136`](orchestrator/services.py#L136)、[`services.py:144`](orchestrator/services.py#L144) |
| P1-07 | appid 业务唯一性与 Docker namespace 大小写语义不一致 | `Demo` 与 `demo` 可同时存在但共享 Docker network/service 名称，可能互相覆盖 | [`models.py:60`](orchestrator/models.py#L60)、[`docker_services.py:1050`](orchestrator/docker_services.py#L1050) |
| P1-08 | published port 探测失败后资源和数据库状态不一致 | Docker service 已创建或更新，但 build 失败、service 记录缺失，后续清理和访问查询都可能失真 | [`worker_runtime.py:331`](orchestrator/worker_runtime.py#L331)、[`worker_runtime.py:350`](orchestrator/worker_runtime.py#L350) |
| P1-09 | 显式 `null` PATCH 可以破坏 app 数据 | API 返回 500，Mongo 可能已经写入非法文档，后续 GET 也可能持续 500 | [`models.py:88`](orchestrator/models.py#L88)、[`services.py:112`](orchestrator/services.py#L112)、[`repository.py:322`](orchestrator/repository.py#L322) |

### P2：应在近期迭代修复

| 编号 | 问题 | 主要影响 | 证据 |
| --- | --- | --- | --- |
| P2-01 | `app_access` 返回所有历史 deployment service | 旧 build 的入口和失败部署记录可能继续显示在页面，缺少 current deployment 语义 | [`main.py:395`](main.py#L395)、[`repository.py:436`](orchestrator/repository.py#L436) |
| P2-02 | 未知字段和空 PATCH 静默成功 | 拼写错误被忽略，空请求仍刷新 `updated_at`，调用方无法判断是否真的修改 | [`models.py:16`](orchestrator/models.py#L16)、[`services.py:114`](orchestrator/services.py#L114) |
| P2-03 | 构建实际 checkout commit 没有形成闭环 | `EXPECTED_COMMIT` 未从 worker 传入 Jenkins，artifact 的 `source.actual_commit` 也没有可靠写回 BuildJob | [`jenkins.py:75`](orchestrator/jenkins.py#L75)、[`orchestrator-build.groovy:22`](jenkins/orchestrator-build.groovy#L22)、[`jenkins.py:136`](orchestrator/jenkins.py#L136) |
| P2-04 | Build 没有保存 Compose snapshot | 构建期间 app 配置改变时，旧 artifact 会与当前 Compose 比较，导致合法构建被拒绝或状态不一致 | [`worker_runtime.py:306`](orchestrator/worker_runtime.py#L306)、[`worker_runtime.py:476`](orchestrator/worker_runtime.py#L476) |
| P2-05 | 构建 ref 可以和 Compose 来源不一致 | 代码来自覆盖后的分支，而 Compose 仍来自 app 当前配置，导致代码与部署配置不匹配 | [`services.py:434`](orchestrator/services.py#L434) |
| P2-06 | Compose 能力预检过晚 | app 创建和 build 创建只检查结构，直到 Jenkins 完成后才发现 Swarm 不支持字段，浪费构建资源 | [`services.py:70`](orchestrator/services.py#L70)、[`services.py:434`](orchestrator/services.py#L434)、[`docker_services.py:356`](orchestrator/docker_services.py#L356) |
| P2-07 | readiness 只判断 task `running`，不判断 health | `healthcheck` 或 `service_healthy` 不会成为部署闸门，应用可能“运行但不可用” | [`docker_services.py:273`](orchestrator/docker_services.py#L273)、[`docker_services.py:599`](orchestrator/docker_services.py#L599) |
| P2-08 | published port 分配存在检查与创建之间的竞争窗口 | 并发部署可能同时选择同一个端口，造成冲突或入口不稳定 | [`docker_services.py:750`](orchestrator/docker_services.py#L750) |
| P2-09 | 部署失败路径不是事务性的 | worker 在镜像、service、状态、事件写入之间崩溃时可能留下半完成状态，恢复任务还可能重复执行 | [`worker_runtime.py:310`](orchestrator/worker_runtime.py#L310)、[`worker_runtime.py:335`](orchestrator/worker_runtime.py#L335) |
| P2-10 | 缺少 app/build/alert 生命周期接口 | 没有 app 删除、Docker 资源清理、build cancel/retry、alert resolve，失败资源和历史记录容易长期残留 | [`main.py:207`](main.py#L207)、[`models.py:39`](orchestrator/models.py#L39) |
| P2-11 | Jenkins 使用 shallow fetch，源码能力覆盖有限 | 依赖 submodule、Git history、Git LFS 或完整仓库上下文的项目可能构建失败 | [`orchestrator-build.groovy:73`](jenkins/orchestrator-build.groovy#L73) |

### P3：工程与安全加固项

| 编号 | 问题 | 主要影响 | 证据 |
| --- | --- | --- | --- |
| P3-01 | production 配置允许默认认证密钥 | 直接使用非 Compose 部署方式时可能误用 `local-worker-secret` 和默认 JWT secret | [`config.py:15`](orchestrator/config.py#L15)、[`config.py:59`](orchestrator/config.py#L59) |
| P3-02 | CORS 全开放 | 任意 Origin、方法和请求头均允许，生产暴露时扩大跨域攻击面 | [`main.py:181`](main.py#L181) |
| P3-03 | 缺少细粒度 RBAC | 所有有效 conductor token 可操作所有 app 和基础设施资源 | [`main.py:142`](main.py#L142) |
| P3-04 | Docker socket 直接暴露给 worker | worker 进程一旦被攻破，通常等价于获得宿主机 Docker 控制权 | [`docker-compose.yml:83`](docker-compose.yml#L83) |
| P3-05 | JWT refresh token 无撤销/轮换机制 | token 泄露后缺少主动失效能力 | [`main.py:202`](main.py#L202) |
| P3-06 | Web UI 将 token 保存在 `localStorage` | XSS 或浏览器扩展风险下 token 可被读取 | [`webui/app.js:11`](webui/app.js#L11) |
| P3-07 | 依赖和基础镜像没有锁定 | 构建结果可能随上游版本变化，供应链复现能力不足 | [`requirements.txt:1`](requirements.txt#L1)、[`Dockerfile:1`](Dockerfile#L1) |
| P3-08 | 缺少 CI、静态检查、类型检查、漏洞扫描和覆盖率门禁 | 回归质量依赖本地执行，缺陷容易在合并和部署阶段才被发现 | 仓库未发现对应 CI/门禁配置 |
| P3-09 | 测试主要使用 fake adapter，真实 E2E 和故障恢复覆盖不足 | 真实 Mongo、Redis/Celery、Jenkins、Harbor、Swarm 的集成错误可能未被自动发现 | `tests/` 现有测试结构 |

## 4. 已确认的功能覆盖能力

### 已覆盖的主要能力

- GitLab 分支/引用解析。
- 通过完整 Git clone 固定到 resolved commit 后，在本地递归搜索 Compose 文件。
- 支持常见文件名：`compose.yaml`、`compose.yml`、`docker-compose.yml`、`docker-compose.yaml`。
- 根目录 Compose 优先；多个嵌套候选文件会返回歧义错误。
- app 创建时自动发现 Compose，或通过 `app_sync` 更新 Compose、来源文件和来源 commit。
- Jenkins 参数化构建、队列轮询、构建状态持久化和结果 artifact 校验。
- Harbor 镜像推送及 Docker Swarm service 部署。
- Docker service 的 environment、ports、restart、resources、healthcheck 等有限字段映射。
- `app_access` 返回 published ports、访问 URL、region 和无入口原因。
- Web UI 支持 app 管理、Compose 编辑、仓库同步、构建轮询、构建历史、访问入口和模板管理。
- app environment 支持 Fernet 加密存储；事件和部分诊断信息会避免直接输出环境变量值。

### 明确的 Compose/交付边界

当前不是完整的 `docker compose up` 替代品。以下能力被拒绝或不完整支持：

- `env_file`
- `configs`、`secrets`
- 自定义 `networks`
- `profiles`
- `entrypoint`
- `devices`、`gpus`
- `privileged`
- `container_name`
- `logging`
- `read_only`
- 节点 placement、update/rollback 策略
- 相对 bind mount

另外：

- GitLab Compose discovery 是完整 clone，但 Jenkins 构建阶段仍使用 `git fetch --depth 1`。
- submodule、Git LFS、Git history 依赖和大仓库磁盘/资源配额没有完整覆盖。
- image-only Compose 服务可以绕过源码 checkout，这是当前设计，需要在 API 和 UI 中明确标识。
- `depends_on: service_healthy` 目前只用于依赖顺序和输入校验，不会阻塞 rollout 等待真正健康。

## 5. 测试覆盖评价

当前 222 项测试全部通过，说明以下模块的离线正常路径较完整：

- GitLab adapter
- Jenkins adapter
- Docker service adapter
- worker runtime
- FastAPI smoke API
- 模板和技术栈
- Web UI 逻辑与浏览器交互
- 多个复杂 Compose fixture

但以下高风险场景没有形成自动化回归：

1. 并发 Jenkins trigger、并发 poller 和 recovery race；
2. Docker service 更新失败后的真实 rollback；
3. 两个 app 使用同名 volume 的隔离；
4. Jenkins artifact 对顶层 volumes、secrets、healthcheck 的保真；
5. app_sync 与 PATCH 并发；
6. explicit-null、unknown-field、empty-PATCH；
7. Mongo 与 Memory repository 的行为一致性；
8. worker 崩溃后的中间状态恢复；
9. 端口分配竞争；
10. 真实 API 与 Web UI 的联合测试；
11. TLS、凭据轮换、镜像签名、跨节点卷驱动和备份恢复。

## 6. 推荐修复路线

### 第一阶段：先消除 P1

1. 为 build trigger、poll、deployment 增加 Mongo 条件更新、lease 和 fencing token。
2. 将部署改为 revision/blue-green，或保存旧 spec 并实现显式 rollback。
3. 按 app 隔离普通 volume，保留 external volume 的显式全局语义。
4. Jenkins artifact 完整保留 Compose 顶层结构，并校验 volume 拓扑。
5. 禁止 Compose 明文秘密进入 API、Jenkins 参数和 artifact；增加脱敏或 secret 引用机制。
6. 为 app_sync 增加 optimistic concurrency，冲突返回 `409`。
7. 强制 appid 归一化为小写，或使用区分大小写的稳定 hash 生成 Docker 名称。
8. 修复 `null` PATCH，确保非法更新不会先写入数据库再失败。

### 第二阶段：修复一致性和生命周期

1. 为 BuildJob 保存 Compose snapshot/hash、repository、requested ref、resolved commit。
2. 只对当前成功 deployment 提供 `app_access`。
3. 接通 Jenkins `EXPECTED_COMMIT` 和 artifact `source.actual_commit`。
4. 在 app_create、app_sync、build_create 时执行 Compose deployability preflight。
5. 增加健康检查、ready 状态和部署失败清理。
6. 增加 app 删除、build cancel/retry、alert resolve 和 Docker 资源清理 API。

### 第三阶段：工程和安全加固

1. production 拒绝默认密钥，限制 CORS origin，增加 RBAC 和 token 撤销/轮换。
2. 增加 CI、lint、类型检查、依赖漏洞扫描和覆盖率阈值。
3. 固定依赖和基础镜像版本，建立可复现构建。
4. 增加真实基础设施 E2E、并发测试、故障恢复测试和备份恢复演练。

## 7. 结论

系统已经具备一条可运行的通用交付链路，但当前更适合测试环境和受控的低并发场景。发布前最需要优先处理的是：

- 并发触发与部署幂等；
- Swarm 更新失败回滚；
- volume 和 app namespace 隔离；
- Compose artifact 保真；
- 秘密保护；
- app_sync/PATCH 数据一致性。

本文件是审计结果记录，不代表其中问题已经修复。后续每完成一项修复，应补充对应回归测试，并重新运行全量测试和真实隔离环境验收。
