# Docker-Jenkins Orchestrator 系统设计

## 1. 目标与范围

本项目接收 conductor-app 对 user-app 的编排请求，完成认证、配置保存、构建任务排队、基础镜像模板组合以及后续 Jenkins、Harbor 和 Docker Services 的适配。`appid` 是跨模块关联 user-app、构建任务、日志、镜像、服务和告警的唯一业务句柄。

首个可运行版本采用“同步 API + 可替换后台任务接口”的分层结构：API 和领域规则可以在没有外部服务时运行和测试；生产环境通过配置启用 MongoDB、Celery、Jenkins、Harbor 和 Docker Services 适配器。

## 2. 设计原则

- 外部系统隔离：Jenkins、Harbor、GitLab、Docker Services 都只能通过 adapter 访问，领域层不依赖 SDK。
- 失败可观测：每个任务有明确状态、错误信息和时间戳；API 不吞掉异常。
- 数据隔离：基础镜像目录与用户构建镜像目录分开，避免用户数据覆盖平台数据。
- 凭据安全：凭据只从环境变量或部署密钥注入，仓库不保存 `auth.txt` 中的值。
- MVP 可演进：默认内存仓储用于本地开发，仓储协议保持 MongoDB 实现可替换。

## 3. 逻辑分层

```text
conductor-app
      |
  FastAPI API  -- JWT dependency -- 领域服务 -- Repository
      |                              |
   schemas                    TaskDispatcher -> Celery (生产)
                                     |
                              External adapters
                         Jenkins / Harbor / Docker Services
```

目录约定：

- `main.py`：应用装配、HTTP 路由和健康检查。
- `orchestrator/`：领域模型、认证、仓储协议、模板服务和任务服务。
- `templates/`：可组合的静态技术栈模板及组合关系定义。
- `tests/`：单元测试和 API 冒烟测试。
- `docs/`：设计与运维文档。

## 4. 认证与 API

`POST /api/connect` 接受 `worker-name` 和 `worker-secret`，与 `ORCHESTRATOR_WORKER_NAME`、`ORCHESTRATOR_WORKER_SECRET` 常量时间比较，返回 HS256 JWT。JWT 使用 `ORCHESTRATOR_JWT_SECRET` 签名，包含 `sub`、`iat`、`exp` 和 `scope`。除 `/api/connect`、`/healthz` 外，所有路由必须验证 JWT。

首批路由：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/healthz` | 进程存活检查，不需要 JWT |
| POST | `/api/connect` | conductor 注册并取得 JWT |
| POST | `/api/apps` | 创建 user-app |
| GET | `/api/apps/{appid}` | 查询 user-app |
| POST | `/api/apps/{appid}/builds` | 创建构建任务并排队 |
| GET | `/api/builds/{build_id}` | 查询构建任务状态 |
| GET | `/api/templates` | 列出可用基础模板 |
| POST | `/api/templates/compose` | 根据组件和调用关系生成 compose |

错误统一返回 `{"detail": "..."}`；输入错误为 422，缺失/无效认证为 401，资源不存在为 404，重复 appid 为 409。

## 5. 核心数据模型

- `UserApp`：`appid`、名称、GitLab 仓库 URL、环境变量（敏感值只在持久层加密/脱敏）、模板组件、创建和更新时间。
- `BuildJob`：`build_id`、`appid`、状态（`queued/running/succeeded/failed`）、提交引用、Jenkins 任务标识、镜像列表、错误和时间戳。
- `BaseImage`：镜像名、版本、Harbor 地址、架构、最后同步时间和状态。基础镜像与用户镜像使用不同集合/命名空间。
- `ServiceTemplate`：组件名、镜像、默认端口、环境变量、依赖关系和健康检查。

所有 ID 在 API 边界使用字符串；时间统一 UTC ISO-8601。

## 6. 模板组合

模板使用 YAML/JSON 可审查的静态定义。组件名采用规范化 key（如 `python`、`mysql`、`react`），组合请求显式提供组件和可选 `depends_on`。组合器负责：

1. 校验组件存在且无重复服务名；
2. 合并默认镜像、端口、环境变量和健康检查；
3. 生成 Docker Compose 规范 `version: "3.9"`；
4. 将调用关系转换为 `depends_on`，拒绝未知依赖和循环；
5. 返回可直接保存到 user-app 的结构化文档。

首批模板覆盖 Python、Go、Java、C++、React、Vue、Next.js、HTML/CSS/JS、MongoDB、MySQL、PostgreSQL、Redis、MinIO、etcd。每个组件提供 2~3 个主流版本的基础镜像清单；镜像拉取/同步由 Celery 任务负责，不能在 API 请求中阻塞。

## 7. 构建任务状态机

API 创建 `queued` 任务并调用 `TaskDispatcher`。Celery worker 后续执行：拉取代码 -> 渲染变量 -> 触发 Jenkins -> 等待构建 -> 推送/登记镜像 -> 部署 Docker Services -> 更新状态。任何步骤失败进入 `failed` 并保留可读错误；成功进入 `succeeded`。状态只能按上述流程前进，重复回调必须幂等。

## 8. 外部适配器与配置

配置项使用环境变量：`ORCHESTRATOR_WORKER_NAME`、`ORCHESTRATOR_WORKER_SECRET`、`ORCHESTRATOR_JWT_SECRET`、`MONGODB_URL`、`JENKINS_URL`、`JENKINS_USER`、`JENKINS_PASSWORD`、`HARBOR_URL`、`HARBOR_USER`、`HARBOR_PASSWORD`、`CELERY_BROKER_URL`。生产部署通过 secret manager 注入。默认开发模式不连接外部服务。

适配器至少提供 `trigger_build`、`get_build_status`、`push_base_image`、`deploy_service` 等协议；网络错误要转换成领域可记录的异常，不能泄漏密码。

## 9. 功能拆分与验收

1. **基础 API 与配置**：应用启动、`/healthz`、配置校验；验收为健康检查冒烟通过。
2. **认证**：连接换 JWT，受保护路由拒绝无 token/错误 token；验收为正反向 API 测试通过。
3. **user-app 与构建任务**：创建、查询、幂等校验和排队；验收为完整生命周期冒烟通过。
4. **模板组合**：模板清单、组合、依赖校验；验收为 Python+MongoDB+React 及错误依赖测试通过。
5. **持久化与后台适配**：MongoDB/Celery/Jenkins/Harbor/Docker Services 的协议实现；验收为 mock adapter 集成测试通过。
6. **基础镜像同步与部署加固**：Celery 定时同步、重试、结构化日志、容器化运行说明。

每个功能点完成后先运行定向冒烟测试，再运行全量测试，提交信息必须说明变更、验证命令和已知限制。

## 10. 非目标与后续决策

本阶段不在 API 中实现任意 shell 执行、不把 Jenkins/Harbor 真实凭据提交到 Git、不承诺所有云厂商 Docker Services 的统一细节。真实 MongoDB/Celery 连接和生产 TLS 在适配器阶段通过环境配置落地。
