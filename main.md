# Agent 交接手册：10.32.12.110 测试发布环境

> 更新时间：2026-11-04（Asia/Shanghai）。这份说明面向接手本仓库开发、验证并发布到共享测试环境的 Agent。文中区分了“仓库配置”和“现场只读核验”；任何实时状态都应在操作前重新确认。

## 1. 快速结论

- **目标环境是 `.110`，不是 `.111`。** `.110` 是共享测试控制面，控制台/API 为 `http://10.32.12.110:18080`；`.111` 是另一个隔离部署，细节见 [`docs/remote-deployment.md`](docs/remote-deployment.md)，不要混用它的地址或部署参数。
- 本次只读核验时间为 2026-11-04：`.110` 的 `/healthz` 和 `/ui/` 均返回 HTTP 200；OpenAPI `info.version` 为 `0.2.0`。
- `.110` 当前运行的 OpenAPI 有 Build 资源接口，但**没有**本仓库现有源码中的 Codex 配置、静态检查、动态检查路由。这表示 `.110` 上运行版本与当前源码存在差异；健康检查通过不代表最新功能已发布。
- 仓库截至本次接手的 `main` HEAD 是 `3fb429c`。工作区原先已有未跟踪验收文档 `docs/acceptance-20261027-code-server-homepage-dashboard.md`；迁移或清理工作区时须保留并先辨明归属。
- `.110` 的最后一份详细发布记录是 2026-10-15，见 [`docs/test-release-20261015.md`](docs/test-release-20261015.md)。不能把历史发布记录当成当前版本证明。
- 凭据不在本文中。操作凭据由工作区父目录的 `../auth.txt`（当前常见绝对路径 `/worker_space/orchestrator/auth.txt`）和 `.110` 主机上的 `/opt/orchestrator-test/.env` 管理；仅通过获准的安全通道读取，不打印、不粘贴、不提交。

## 2. 环境与发布能力

### `.110` 共享测试环境

| 项目 | 已知信息 |
| --- | --- |
| 控制台/API | `http://10.32.12.110:18080/ui/`；健康检查 `GET /healthz` |
| 编排 | FastAPI + Celery Worker/Beat；Redis 是 broker，MongoDB 保存业务状态 |
| 发布链路 | API → Redis/Celery → Worker → Jenkins → Harbor → Docker Swarm；Worker 回读 Jenkins 制品、部署服务并执行就绪/端口探测 |
| Jenkins Job | `apps-orchestrator/orchestrator-real-delivery-20260920`（以现存测试配置和 Jenkins 为准） |
| Jenkins / Harbor 地址 | 准备脚本默认 Jenkins `10.17.158.156`、Harbor `10.17.158.118`；实际地址、用户和密码从授权配置读取，不在本文复制 |
| 用户服务端口 | 默认从 `18000`–`18999` 分配；实际入口通过 `app_access` / Build 资源接口查询，冲突时端口可能变化 |
| 凭据文件 | `../auth.txt` 位于仓库外；长期运行环境 dotenv 是 `/opt/orchestrator-test/.env`（权限应为 `0600`） |
| 最新有据可查发布 | `docs/test-release-20261015.md` 记录了发布目录、备份、回滚与验收；应先核实主机 `current-release` 实际指向及服务状态 |

`.110` 的只读核验命令示例：

```bash
curl --fail http://10.32.12.110:18080/healthz
curl --fail http://10.32.12.110:18080/openapi.json
```

仅 `/healthz` 返回成功不能证明 Worker、Beat、Jenkins、Harbor、Docker Swarm 或当前代码版本可用。部署前后至少核验 API、Worker、Beat 健康状态、Celery `inspect ping`、OpenAPI 路由以及一轮经过批准的隔离构建。

### `.111` 是独立环境

[`docs/remote-deployment.md`](docs/remote-deployment.md) 记录 `.111` 的 API/UI 为 `http://10.32.12.111:8088`。该文档记录的状态是 GitLab/Jenkins/Harbor 集成凭据未配置、Swarm inactive，因此不能将其当作 `.110` 的真实构建环境，也不能假定它现在的状态未变。

## 3. 安全发布流程

### 开发前

1. 从仓库根目录开始，先执行 `git status --short --branch`、`git log -1 --oneline`，确认分支、提交和未提交文件。保留已有未跟踪的验收文档，不要把无关文件带入提交。
2. 阅读 [`README.md`](README.md)、[`docs/architecture.md`](docs/architecture.md)、[`docs/api-call-flow.md`](docs/api-call-flow.md) 和与本次功能直接相关的 `docs/` 文档。
3. 本地先跑与改动相关测试；通常全量回归为 `pytest -q`。Pipeline 修改还必须按 [`docs/pipeline-validation.md`](docs/pipeline-validation.md) 使用 Jenkins Pipeline gate 校验完整 Groovy Pipeline。

### 发布到 `.110`

1. **先检查，不初始化、不重建环境。** 只读确认 `/opt/orchestrator-test/.env`、当前发布目录、活动 Build、Mongo/Redis 数据、工作区和 Swarm 服务状态。共享环境禁止重新运行 `scripts/prepare_test_environment.py`：它会生成新身份/密钥/数据库名，不是更新现有环境的命令。
2. 确认无活动构建/清理后，按 `docs/test-release-20261015.md` 的发布材料和主机上的 `current-release` 入口执行受控更新。现存运维入口记为：

   ```bash
   /opt/orchestrator-test/current-release/compose.sh ps
   ```

   发布时保留既有 dotenv、Mongo 数据、JWT/Fernet 密钥、Redis 数据、上传 workspace 和回滚材料；只按发布方案更新 API、Worker、Beat。不要只升级 Jenkins Pipeline 或 Worker 而造成接口/制品契约不一致。
3. 修改 `jenkins/orchestrator-build.groovy` 前，先执行只校验的 gate：

   ```bash
   python3 scripts/jenkins_pipeline_gate.py --env-file /opt/orchestrator-test/.env
   ```

   只有经确认需要更新**固定测试 Job**时才使用 `--apply`。Gate 会校验完整 Pipeline、更新后精确回读并保留其他 Job 配置；不要绕过失败结果手工覆盖 Job。
4. 部署后检查容器健康、`/healthz`、`/ui/`、关键 OpenAPI 路由、Worker/Beat 与 Jenkins 连接。再用唯一测试 APP / Build ID 验证静态门禁、Jenkins 构建、Harbor 制品、Swarm task `running`、动态路由及访问入口。HTTP 202 只表示 Build 已受理，必须轮询直到终态。
5. 用本轮 `build_id` 查询并清理本轮资源，确认 Build 资源报告清理完成。不得删除别的 APP/Build 资源。

### 禁止的破坏性操作

- 不要对共享环境运行 `docker compose down`、`docker system prune`、`docker swarm init/reset` 或 Harbor 全局 GC。
- 不要重跑环境准备脚本、覆盖/重置 `.env`、换 Mongo 数据库、JWT/Fernet key、Redis 数据或 worker identity。
- 不要停止既有 Apps 服务，不要将测试失败或清理操作作用于未明确授权的历史 Build。
- Build 级清理只使用 API/控制台的 `build_id`；失败或部分失败应检查资源报告后重试该 Build 清理，不做全局 Docker 清理。
- `Worker` 可访问 Docker socket，权限接近宿主机 root；只对可信仓库执行构建和 Agent 检查。

## 4. 构建和检查规则

- 所有待构建服务必须提供有效的本地 `build.context` 和 `build.dockerfile`，image-only Compose 不符合当前构建合同；Dockerfile 必须存在于 context 内。
- 新 Build 要求关联的完整 Git commit 和 `require_route`，并先通过静态检查；静态检查通过不代表运行时成功。
- Jenkins 必须归档 `orchestrator-result.json` 并推送 Harbor；Worker 再以本次制品部署独立 Swarm 资源。终态成功还要求 task ready，并在配置公开 host 后通过端口/动态检查。
- `docker build` 当前**不带 `--pull`**，以复用 Jenkins builder 本地缓存基础镜像；改基础镜像时需明确拉取策略。提交时间为 2026-10-21 的 `c74a46b`。
- Dockerfile `RUN` 需要的网络代理不等同于 Docker daemon 代理。Jenkins builder 的 daemon proxy 与 Docker client `config.json` 的 `proxies` 配置分别处理；按 [`docs/test-environment.md`](docs/test-environment.md) 验证，不要把宿主机 auth 或 credential helper 复制进临时 Docker 配置。
- 外网服务访问入口以运行时 `app_access` / Build 资源报告返回的实际端口为准；不要假设 Compose 请求端口未变化。

完整流程和示例见：

- [`docs/test-environment.md`](docs/test-environment.md)：准备测试环境、端口、仓库同步及共享环境操作边界。
- [`docs/test-release-20261015.md`](docs/test-release-20261015.md)：`.110` 最近有记录的发布、验收、影响与回滚材料。
- [`docs/pipeline-validation.md`](docs/pipeline-validation.md)：Jenkins gate 与真实链路验收。
- [`docs/route-checks.md`](docs/route-checks.md)：静态/动态检查门禁。
- [`docs/api-call-flow.md`](docs/api-call-flow.md) / [`docs/api-reference.md`](docs/api-reference.md)：API 调用流程和接口合同。
- [`docs/live-delivery-audit.md`](docs/live-delivery-audit.md)：历史实机交付、错误归因和资源核验；其中 APP/Build ID 是历史证据，不得当成本轮结果。
- [`docs/acceptance-20261027-code-server-homepage-dashboard.md`](docs/acceptance-20261027-code-server-homepage-dashboard.md)：`.111` 上三项目隔离复测，**不是** `.110` 本项目发布验收。

## 5. 环境变量清单

### 5.1 `.110` 测试环境的必需输入

`scripts/prepare_test_environment.py` 从操作者授权的 `auth.txt` 读取 Jenkins、Harbor、MongoDB 凭据，生成随机 worker/client secret、JWT secret、Fernet key、Redis 密码和隔离数据库名，并以 `0600` 写入 dotenv。脚本可通过 `--auth-file`、`--runtime-dir`、`--run-id`、`--worker-region`、`--jenkins-host`、`--harbor-host`、`--jenkins-job`、`--api-port`、`--api-bind-address`、`--api-advertise-host`、`--redis-port` 覆盖非秘密参数；默认值以脚本 `--help` 和源码为准。

Compose 对新测试部署强制要求的值如下（由脚本生成或从 auth 注入）：

| 变量 | 用途/来源 |
| --- | --- |
| `ORCHESTRATOR_WORKER_NAME` | 隔离 Worker / queue / 资源前缀；脚本生成 |
| `ORCHESTRATOR_WORKER_SECRET` | conductor client secret；脚本生成，不能输出 |
| `ORCHESTRATOR_JWT_SECRET` | JWT 签名密钥；脚本生成，不能轮换既有环境值 |
| `ORCHESTRATOR_MONGODB_URL` | 测试 MongoDB 连接；来自授权 `auth.txt`，勿打印 |
| `ORCHESTRATOR_MONGODB_DATABASE` | 每轮隔离数据库名；脚本生成，现存共享环境不可重置 |
| `ORCHESTRATOR_DATA_ENCRYPTION_KEY` | Fernet 加密密钥；脚本生成；必须和所加密数据生命周期一致 |
| `ORCHESTRATOR_REDIS_PASSWORD` | 本地 Redis / Celery broker 密码；脚本生成 |
| `ORCHESTRATOR_JENKINS_URL`、`ORCHESTRATOR_JENKINS_USER`、`ORCHESTRATOR_JENKINS_PASSWORD` | Jenkins API；URL由 host 参数构造，凭据来自 `auth.txt` |
| `ORCHESTRATOR_JENKINS_JOB_NAME` | 固定的测试交付 Job；默认发现 `apps-orchestrator/orchestrator-real-delivery-20260920` |
| `ORCHESTRATOR_HARBOR_URL`、`ORCHESTRATOR_HARBOR_USER`、`ORCHESTRATOR_HARBOR_PASSWORD` | Harbor Registry；凭据来自 `auth.txt` |

Mongo、Jenkins、Harbor 与 GitLab 的真实地址/凭据以当前授权文件、DNS/网络和远端服务核验结果为准。不要将凭据复制到本文、命令行参数、终端记录或 Git。

### 5.2 Codex Provider 启动要求与已发现的部署缺口

当前源码在 `ORCHESTRATOR_ENVIRONMENT=production` 时会于启动阶段验证 Codex Provider。数据库没有有效配置时，环境中必须提供：

| 必需变量 | 说明 |
| --- | --- |
| `CODEX_PROVIDER_NAME` | Provider 展示名称 |
| `CODEX_PROVIDER_ENDPOINT` | 兼容 Codex CLI 的 Provider API endpoint |
| `CODEX_API_KEY` | API Key；仅 API 服务用于首次写入，入库加密，Worker 从 Mongo 读取，不应明文注入 Worker |
| `CODEX_DEFAULT_MODEL` | 默认模型 |
| `CODEX_DEFAULT_EFFORT` | 默认推理强度 |

可选扩展：`CODEX_PROVIDER_ID`、`CODEX_PROVIDER_WIRE_API`、`CODEX_PROVIDER_REQUIRES_OPENAI_AUTH`、`ORCHESTRATOR_CODEX_EXECUTABLE`、`ORCHESTRATOR_CODEX_HTTPS_PROXY`。`OPENAI_API_KEY` 是另一个可选 CLI 认证变量，不替代自定义 Provider 的 `CODEX_API_KEY`。模型代理只应通过专用的 `ORCHESTRATOR_CODEX_HTTPS_PROXY` 传给 Codex 子进程，不能设成全局 `HTTPS_PROXY` 以免 GitLab/Jenkins/Harbor 请求误走代理。

**部署前必须处理的差异：** 当前 `docker-compose.test.yml` 将环境设为 `production`，但它没有把 `CODEX_PROVIDER_NAME`、`CODEX_PROVIDER_ENDPOINT`、`CODEX_DEFAULT_MODEL`、`CODEX_DEFAULT_EFFORT` 和 API 侧 `CODEX_API_KEY` 传给 API；`scripts/prepare_test_environment.py` 也没有生成或读取这些 Provider 值。其新建的 Mongo 数据库为空。因此用当前脚本从零启动一套测试环境时，除非单独安全注入这些变量或先有有效 DB 配置，API 会因 Codex 配置缺失而启动失败。修复应落在 Compose/准备配置和测试中，不能把密钥写入仓库。现有 `.110` 健康并不表示它已部署当前 Codex 功能（现场 OpenAPI 未发现该路由）。

### 5.3 当前 `.env.example` 全量变量索引

`.env.example` 是仓库默认值和完整配置名的权威清单；下表按功能列出其变量。除第 5.1 节及 Codex 启动条件外，多数变量有默认值；若要确认默认值/约束，直接读 `.env.example`、`docker-compose.test.yml` 和 `orchestrator/config.py`，不要另造第二份默认配置。

| 功能 | 变量 |
| --- | --- |
| 镜像构建/运行身份 | `ORCHESTRATOR_DEBIAN_MIRROR`, `ORCHESTRATOR_ENVIRONMENT`, `ORCHESTRATOR_WORKER_NAME`, `WORKER_REGION`, `ORCHESTRATOR_WORKER_SECRET`, `ORCHESTRATOR_JWT_SECRET`, `ORCHESTRATOR_JWT_EXPIRE_MINUTES`, `ORCHESTRATOR_JWT_REFRESH_EXPIRE_DAYS` |
| Mongo/数据加密 | `ORCHESTRATOR_STORAGE_BACKEND`, `ORCHESTRATOR_MONGODB_URL`, `ORCHESTRATOR_MONGODB_DATABASE`, `ORCHESTRATOR_MONGODB_TIMEOUT_MS`, `ORCHESTRATOR_DATA_ENCRYPTION_KEY` |
| Celery/调度 | `ORCHESTRATOR_TASK_DISPATCHER`, `ORCHESTRATOR_CELERY_BROKER_URL`, `ORCHESTRATOR_CELERY_BUILD_QUEUE`, `ORCHESTRATOR_CELERY_IMAGES_QUEUE`, `ORCHESTRATOR_CELERY_POLL_INTERVAL_SECONDS`, `ORCHESTRATOR_CELERY_MAX_POLL_ATTEMPTS`, `ORCHESTRATOR_CELERY_RECOVERY_INTERVAL_SECONDS`, `ORCHESTRATOR_BASE_IMAGE_SYNC_INTERVAL_HOURS` |
| Codex | `OPENAI_API_KEY`, `ORCHESTRATOR_CODEX_HTTPS_PROXY`, `ORCHESTRATOR_CODEX_EXECUTABLE`, `CODEX_PROVIDER_NAME`, `CODEX_PROVIDER_ENDPOINT`, `CODEX_API_KEY`, `CODEX_DEFAULT_MODEL`, `CODEX_DEFAULT_EFFORT`, `CODEX_PROVIDER_ID`, `CODEX_PROVIDER_WIRE_API`, `CODEX_PROVIDER_REQUIRES_OPENAI_AUTH` |
| 路由检查 | `ORCHESTRATOR_ROUTE_CHECK_LEASE_SECONDS`, `ORCHESTRATOR_ROUTE_PROBE_TIMEOUT_SECONDS`, `ORCHESTRATOR_ROUTE_PROBE_MAX_RESPONSE_BYTES` |
| GitLab/Jenkins/Harbor | `ORCHESTRATOR_GITLAB_URL`, `ORCHESTRATOR_GITLAB_TOKEN`, `ORCHESTRATOR_JENKINS_URL`, `ORCHESTRATOR_JENKINS_USER`, `ORCHESTRATOR_JENKINS_PASSWORD`, `ORCHESTRATOR_JENKINS_JOB_NAME`, `ORCHESTRATOR_HARBOR_URL`, `ORCHESTRATOR_HARBOR_USER`, `ORCHESTRATOR_HARBOR_PASSWORD`, `ORCHESTRATOR_HARBOR_CA_FILE`, `ORCHESTRATOR_HARBOR_PROJECT`, `ORCHESTRATOR_HARBOR_BOOT_IMAGES_PROJECT` |
| Docker/部署/清理 | `ORCHESTRATOR_DOCKER_BASE_URL`, `ORCHESTRATOR_DOCKER_SOCKET_PATH`, `ORCHESTRATOR_DOCKER_SERVICES_NETWORK`, `ORCHESTRATOR_DEPLOYMENT_READINESS_TIMEOUT_SECONDS`, `ORCHESTRATOR_DEPLOYMENT_READINESS_POLL_INTERVAL_SECONDS`, `ORCHESTRATOR_DEPLOYMENT_PORT_RANGE_START`, `ORCHESTRATOR_DEPLOYMENT_PORT_RANGE_END`, `ORCHESTRATOR_CLEANUP_TIMEOUT_SECONDS`, `ORCHESTRATOR_CLEANUP_DOCKER_TARGETS`, `ORCHESTRATOR_EXTERNAL_REQUEST_TIMEOUT_SECONDS`, `ORCHESTRATOR_GIT_CLONE_TIMEOUT_SECONDS`, `ORCHESTRATOR_DOCKER_PROXY_CONFIG`（Jenkins builder 可选） |
| 对外访问/API 监听 | `ORCHESTRATOR_PUBLIC_HOST`, `ORCHESTRATOR_PUBLIC_SCHEME`, `ORCHESTRATOR_API_BIND_ADDRESS`, `ORCHESTRATOR_API_HOST_PORT` |
| Workspace | `ORCHESTRATOR_WORKSPACE_HOST_ROOT`, `ORCHESTRATOR_WORKSPACE_DOWNLOAD_BASE_URL`, `ORCHESTRATOR_WORKSPACE_SHARED_STORAGE`, `ORCHESTRATOR_WORKSPACE_MAX_UPLOAD_BYTES`, `ORCHESTRATOR_WORKSPACE_MAX_BYTES`, `ORCHESTRATOR_WORKSPACE_MAX_FILES` |

测试 Compose 另有以下测试部署变量（不是 `.env.example` 的完整生产清单）：`ORCHESTRATOR_REDIS_PASSWORD`（必需）、`ORCHESTRATOR_REDIS_HOST_PORT`（默认 `16379`）、`ORCHESTRATOR_TEST_DATA_PATH`（默认 `/opt/orchestrator-test`）、`ORCHESTRATOR_API_ADVERTISE_HOST`（可选覆盖自动探测地址）、`ORCHESTRATOR_TEST_RUN_ID`（可选固定运行 ID）。测试 Compose 也使用 `.env.example` 中的 `ORCHESTRATOR_API_BIND_ADDRESS` 和 `ORCHESTRATOR_API_HOST_PORT`。

准备/验证脚本可读的辅助变量还包括 `ORCHESTRATOR_AUTH_FILE`（`auth.txt` 路径）、`ORCHESTRATOR_TEST_CHROMIUM_PATH`（浏览器二进制覆盖）、`LIVE_WEBUI_API_URL`、`LIVE_WEBUI_RUN`、`LIVE_E2E_AUTH_FILE`、`LIVE_E2E_JENKINS_JOB`、`LIVE_E2E_RUN`、`LIVE_E2E_API_PORT`、`LIVE_E2E_MONGODB_URL`、`LIVE_E2E_REDIS_URL`、`LIVE_E2E_PROJECTS`。这些只用于对应脚本，不要误当成常驻服务必需配置；运行真实交付脚本前必须先读脚本说明，因为它会创建并清理外部资源。

## 6. 仓库与新 Agent 接手顺序

1. 确认新工作区里有本文件、`README.md`、`docs/`、`jenkins/orchestrator-build.groovy`、`scripts/` 和 `.env.example`。
2. 安全确认 `auth.txt` 和远端 `/opt/orchestrator-test/.env` 是否通过授权渠道可用；不在仓库中复制。新 Agent 若缺少秘密，不得猜测或读取历史终端/日志补齐。
3. 首先查 `git status`，保留工作区中已有未提交改动；目前记录的 HEAD 是 `3fb429c`，另有一份未跟踪验收文档。
4. 阅读第 2 节的版本差异和第 5.2 节 Codex 新环境启动缺口，确认本轮目标是“修复当前 `.110` 发布差异”还是“继续新功能开发”。
5. 开发先局部测试，再全量 `pytest -q`；发布前执行 Jenkins gate（若 Pipeline 变更），计划并核验隔离真实构建。未经用户明确要求，不执行提交、push、部署或破坏性清理。

## 7. 本次核验边界

- 本次对 `.110` 仅执行 `GET /healthz`、`GET /ui/` 和 `GET /openapi.json`；没有登录、创建 APP、提交 Build、访问凭据、修改远端、清理资源或重启服务。
- `.110` 返回 HTTP 200，OpenAPI 版本为 `0.2.0`；Build 资源路由存在，Codex 配置、静态检查、动态检查路由不存在。
- 这只是 2026-11-04 当时的现场状态。每次后续交接/发布都需重新只读核验，不要仅依据本文件推断远端仍健康。
