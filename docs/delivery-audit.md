# 复杂应用交付走查报告

> 本文前半部分记录离线验收；真实 Jenkins、Harbor、MongoDB、Redis/Celery 和
> Docker Swarm 的隔离联调记录见文末和 [live-delivery-audit.md](live-delivery-audit.md)。
> 认证信息没有写入 Git。

## 走查范围

本次走查以项目根目录 `main.txt` 为需求基线，结合当前架构文档、FastAPI、Celery worker、Mongo repository、Jenkins/GitLab/Harbor/Docker 适配器和生产 Compose 配置进行验证。参考项目使用 Immich、Plane、Paperless-ngx、Umami、SearXNG、Open WebUI、Linkwarden 和 Planka 的官方 Compose/部署模板；复杂项目验收使用 memory repository 和 fake Docker/Jenkins client，不拉取镜像，也不连接任何真实外部系统。Mongo 持久化契约由独立 repository 测试覆盖。

验证命令：

```text
pytest -q                         # 106 passed
docker compose config -q          # passed with test environment values
python3 -m compileall -q .        # passed
```

## 已具备的交付能力

- FastAPI 通过 `worker-name`/`worker-secret` 签发 JWT，业务路由要求 Bearer token；应用、构建、事件、镜像、服务和告警均以 `appid` 关联。
- Mongo 是业务状态的权威存储，Redis 只作为 Celery broker；worker/beat 可以从 Mongo 重新发现非终态构建，不依赖 Celery result backend。
- Jenkins queue/build/artifact 轮询、失败告警、重复投递幂等状态推进和 Docker service 记录已有 fake adapter 集成测试。
- 构建输入要求 conductor/用户角色先提供包含 `services` 的 Compose；仓库没有可直接使用的 Compose 时，构建入口明确报错。用户根据通用模板在系统外完成调整并重新提交，系统不会替用户猜测或生成应用编排。
- Docker Swarm 适配器支持 digest 镜像、环境变量、命令、工作目录、挂载、端口、labels、重启策略、replicas/global 模式、健康检查、`shm_size` 和 CPU/内存资源；更新时会按 appid 清理已删除的旧 service。
- 顶层命名卷可携带标准的 `name` 与 `external` 元数据；其他未映射的卷驱动选项仍明确拒绝，避免声称已有 Swarm 卷驱动编排能力。
- 通用复杂项目测试覆盖 Immich（4 服务）、Plane（13 服务）、Paperless-ngx（5 服务）、Umami（2 服务）、SearXNG（2 服务）、Open WebUI（2 服务）、Linkwarden（3 服务）和 Planka（2 服务）的用户角色 Compose；同一条 pipeline 验证服务数、端口、镜像、依赖、健康检查、挂载和 repository 状态记录。
- 原始项目形态的失败测试覆盖缺少 Compose，以及最终 artifact 中仍残留 `build` context、`env_file`、`container_name` 或 `init`；Planka 还验证了“源文件结构已可映射、但用户仍需替换示例密钥”的路径。源 Compose 可以先交给 Jenkins 处理，但交付边界会拒绝未规范化的 artifact，证明系统要求用户/流水线完成外部调整而不是针对项目名称写适配逻辑。
- 每个项目的上游观察、用户输出和 manifest 位于 `tests/fixtures/complex_projects/<project>/`；统一参数化验收位于 `tests/test_complex_project_delivery.py`。
- fixture 完整性测试逐项确认四类用户角色产物存在，manifest 的项目名和服务数与 YAML 一致，且用户输出记录了系统反馈和调整结果。
- fixture manifest 固定了上游分支、源 Compose 路径及对应提交 SHA；测试还调用 Docker SDK 的 service 参数规范化器，确认 fake 调用的字段形状可被真实 SDK 接受。
- 不能安全映射的 `env_file`、secrets/configs、GPU/devices、自定义网络、`container_name` 等字段现在会在创建网络或 service 前明确失败，不再静默丢弃。
- 镜像缺失、非法服务名、依赖环、相对 bind mount（例如 `./data:/data`）以及其他非法参数都会在创建 Docker network 或 service 前拒绝；非法 artifact 不会留下部分基础设施。

## 仍然存在的产品边界

### 高优先级：Jenkins job 仍需在目标环境部署

仓库现在提供了 [jenkins/orchestrator-build.groovy](../jenkins/orchestrator-build.groovy) 作为标准流水线模板：它校验 Compose、构建或提升镜像、推送 Harbor，并归档保留完整 service 拓扑的 `orchestrator-result.json`。模板不能替代目标 Jenkins 的凭据、builder 节点、Docker daemon 和 Harbor/GitLab 网络配置；任意 GitLab 仓库仍需在隔离环境中用实际 job 验收。

### 高优先级：Compose 依赖不是健康就绪闸门

`depends_on` 会被验证并用于依赖优先创建，但 Docker Swarm Service API 没有 Compose 的 `service_healthy` 启动闸门。healthcheck 会进入 service task spec，worker 不会等待数据库/Redis 健康后才标记部署成功。像 Immich 这样的应用必须自身具备连接重试，或后续增加部署后健康检查/回滚流程。

### 中优先级：`env_file` 必须在用户角色/Jenkins 侧展开

Jenkins JSON artifact 没有 Compose `env_file` 路径对应的文件系统上下文。当前适配器会拒绝 `env_file`，要求用户角色在系统外将模板中的变量填入 `environment`，再由 Jenkins 输出 artifact。若 Jenkins job 继续原样传递官方 Compose，构建会安全失败而不是错误部署；这属于明确的输入前置条件。

### 中优先级：Swarm 不是完整 Compose 实现

当前支持范围有意限制在可转换到 Docker Service API 的字段。`secrets/configs`、GPU/devices、custom networks、`profiles`、`read_only`、logging、placement/update/rollback 策略等字段会被拒绝，需要专门的 Docker secret/config 和节点调度设计后才能支持。不能把本适配器当作 `docker compose up` 的替代品。

### 中优先级：真实基础设施和供应链验证仍需环境验收

测试没有连接真实 GitLab、Jenkins、Harbor、MongoDB、Redis 或 Swarm，也没有验证 TLS、凭据轮换、镜像签名/准入策略、跨节点卷驱动和备份恢复。生产上线前至少应执行一次隔离环境的完整构建、失败重试、worker 重启恢复、service 更新/删除和 Mongo 恢复演练。

## 结论

系统已经具备“可测试的端到端编排骨架”和一条可交付的标准路径：GitLab ref -> Jenkins -> artifact -> Docker Swarm -> Mongo 状态。它还不能宣称对 Immich 或任意复杂 Compose 做到无条件的完整交付；实际交付依赖 Jenkins job 展开环境文件、应用自身处理依赖就绪，并避开当前明确拒绝的 Compose 高级能力。上述边界已通过测试和文档固定，后续扩展应先增加对应的 fake/隔离环境验收，再扩大适配器支持范围。

## 真实隔离联调记录

正式三轮运行标识为 `final3-1789363218`，使用 `umami-<uuid>`、`planka-<uuid>` 和
`linkwarden-<uuid>` appid，三轮均成功并完成清理。完整的 appid、build、Jenkins
编号、Harbor digest、Mongo/Celery/Swarm 断言、Linkwarden 根因和最终空态核验见
[live-delivery-audit.md](live-delivery-audit.md)。
