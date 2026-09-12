# 复杂应用交付走查报告

## 走查范围

本次走查以项目根目录 `main.txt` 为需求基线，结合当前架构文档、FastAPI、Celery worker、Mongo repository、Jenkins/GitLab/Harbor/Docker 适配器和生产 Compose 配置进行验证。参考项目使用 Immich、Plane 和 Paperless-ngx 的官方 Compose/部署模板；测试只使用 fake Docker client，不拉取镜像，也不连接任何真实外部系统。

验证命令：

```text
pytest -q                         # 66 passed
docker compose config -q          # passed with test environment values
python3 -m compileall -q .        # passed
```

## 已具备的交付能力

- FastAPI 通过 `worker-name`/`worker-secret` 签发 JWT，业务路由要求 Bearer token；应用、构建、事件、镜像、服务和告警均以 `appid` 关联。
- Mongo 是业务状态的权威存储，Redis 只作为 Celery broker；worker/beat 可以从 Mongo 重新发现非终态构建，不依赖 Celery result backend。
- Jenkins queue/build/artifact 轮询、失败告警、重复投递幂等状态推进和 Docker service 记录已有 fake adapter 集成测试。
- 构建输入要求 conductor/用户角色先提供包含 `services` 的 Compose；仓库没有可直接使用的 Compose 时，构建入口明确报错。用户根据通用模板在系统外完成调整并重新提交，系统不会替用户猜测或生成应用编排。
- Docker Swarm 适配器支持 digest 镜像、环境变量、命令、工作目录、挂载、端口、labels、重启策略、replicas/global 模式、健康检查、`shm_size` 和 CPU/内存资源；更新时会按 appid 清理已删除的旧 service。
- 通用复杂项目测试覆盖 Immich（4 服务）、Plane（13 服务）和 Paperless-ngx（5 服务）的用户角色 Compose；同一条 pipeline 验证服务数、端口、镜像、依赖、健康检查、挂载和 Mongo 状态持久化。
- 原始项目形态的失败测试覆盖缺少 Compose、`build` context 和 `env_file`，证明系统会要求用户角色先完成外部调整，而不是针对项目名称写适配逻辑。
- 每个项目的上游观察、用户输出和 manifest 位于 `tests/fixtures/complex_projects/<project>/`；统一参数化验收位于 `tests/test_complex_project_delivery.py`。
- 不能安全映射的 `env_file`、secrets/configs、GPU/devices、自定义网络、`container_name` 等字段现在会在创建网络或 service 前明确失败，不再静默丢弃。

## 仍然存在的产品边界

### 高优先级：Jenkins job 本身不在本仓库

本项目只实现 Jenkins HTTP 适配器和 `orchestrator-result.json` 合同，没有 Jenkinsfile、流水线脚本、镜像构建安全策略或真实 Jenkins 验收环境。因此当前测试能证明“请求参数、轮询和产物处理”正确，不能证明任意 GitLab 仓库能在真实 Jenkins 中成功构建。交付前仍需要部署并验收对应 Jenkins job。

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
