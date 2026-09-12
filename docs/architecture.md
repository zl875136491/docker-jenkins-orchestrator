# Docker-Jenkins Orchestrator 系统设计

## 1. 目标

本系统接受 conductor-app 对 user-app 的编排请求。每个 user-app 以 `appid` 为全局业务句柄；应用配置、模板、构建、日志、输出、告警、镜像和 Docker Services 部署记录都必须关联该值。

系统负责将 GitLab 仓库、环境变量和 Docker Compose 定义交给 Jenkins 执行 CI/CD，记录构建产物，并将结果部署为 Docker Services。FastAPI 只负责认证、接收命令、查询状态和保存业务数据；所有耗时、轮询和定时工作由 Celery worker 执行。

## 2. 运行拓扑

```text
conductor-app -- JWT --> FastAPI API ------> MongoDB
                              |                |
                              |                +-- user_apps / build_jobs / events
                              |                +-- user_images / base_images / services / alerts
                              v
                        Redis broker
                              |
                              v
                    Celery worker / beat
                 /             |              \
           GitLab API      Jenkins API      Harbor / Docker Engine
```

Redis 仅用于 Celery broker：它不保存业务状态，也不作为 Celery result backend。MongoDB 是任务状态、事件、日志和部署数据的权威来源；worker 重启后从 MongoDB 恢复工作。Celery beat 会定期扫描 `queued`、`validating`、`triggering`、`building`、`deploying` 的记录，并分别重新投递启动或轮询任务。开发测试可显式选择内存仓储和内存 dispatcher，但 Docker Compose 与生产配置必须使用 MongoDB 和 Redis。

## 3. 领域模型与集合

| 集合 | 主键/索引 | 作用 |
|---|---|---|
| `user_apps` | 唯一 `appid` | 仓库、Git 引用、加密环境变量、Compose 和模板选择 |
| `build_jobs` | 唯一 `build_id`，`appid+status`、`status+updated_at` | 构建状态、请求 Git 引用及已解析提交 SHA、Jenkins 队列/构建号、部署引用和错误 |
| `app_events` | `appid+created_at`，`build_id+created_at` | 不可变审计事件、任务日志和外部调用摘要 |
| `user_images` | `appid+build_id+reference` | Jenkins 产出的应用镜像，与基础镜像隔离 |
| `base_images` | 唯一 `source_image` | `boot-images` 项目的同步状态、摘要和错误 |
| `deployment_services` | `appid+build_id+service_name` | Docker Swarm Service ID、镜像、状态和端点 |
| `alerts` | `appid+created_at` | 构建/部署失败及需要人工处理的告警 |

环境变量不会在读 API 或事件中回显；生产 MongoDB 数据使用 `ORCHESTRATOR_DATA_ENCRYPTION_KEY` 加密保存。Jenkins 只在任务执行时获得解密后的变量。

## 4. 认证与 API

`POST /api/connect` 接受 `worker_name` 和 `worker_secret`，使用常量时间比较校验 `ORCHESTRATOR_WORKER_NAME` 与 `ORCHESTRATOR_WORKER_SECRET`，然后签发带 `sub`、`scope`、`iat`、`exp` 的 HS256 JWT。`/healthz` 与 `/api/connect` 外的全部路由都必须携带 Bearer JWT。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 进程存活检查 |
| POST | `/api/connect` | conductor-app 获取 JWT |
| POST / GET / PATCH | `/api/apps`, `/api/apps/{appid}` | 创建、读取、更新 user-app |
| POST | `/api/apps/{appid}/builds` | 创建并投递构建任务 |
| GET | `/api/builds/{build_id}` | 查询任务和当前状态 |
| GET | `/api/apps/{appid}/events` | 查询按时间排序的审计事件/日志 |
| GET | `/api/apps/{appid}/images` | 查询用户镜像 |
| GET | `/api/apps/{appid}/services` | 查询 Docker Services 部署结果 |
| GET | `/api/apps/{appid}/alerts` | 查询告警 |
| GET / POST | `/api/templates`, `/api/templates/compose` | 查询/组合静态技术栈模板 |
| GET / POST | `/api/base-images`, `/api/base-images/sync` | 查询或投递基础镜像同步任务 |

创建或更新 user-app 时可提供已验证的 `compose` 文档，或通过模板组合 API 生成后保存。请求中的 `environment` 仅写入，不在响应中返回值。

## 5. 构建状态机

```text
queued -> validating -> triggering -> building -> deploying -> succeeded
   |           |              |             |             |
   +----------> failed <------+-------------+-------------+
```

每次状态变化都写入 `build_jobs` 和 `app_events`。终态 `succeeded`、`failed` 不允许回退；重复投递或重复 Celery 消息必须通过条件更新保持幂等。

Celery 执行步骤：

1. 读取应用和构建记录，校验 Compose 的服务结构；
2. 可选调用 GitLab API 验证仓库及 Git 引用；解析成功时将提交 SHA 写入 `build_jobs`，Jenkins 使用该 SHA 构建；
3. 调用 Jenkins 参数化任务，传递 `APPID`、仓库、Git 引用/提交 SHA、Compose、环境变量和目标 Harbor 镜像仓库；
4. 定时 Celery 任务轮询 Jenkins queue/build 状态；
5. 成功后读取 Jenkins `orchestrator-result.json` 产物，登记应用镜像；
6. 调用 Docker Engine/Swarm 创建或更新 Services，登记 service ID 和端点；
7. 成功写入 `succeeded`，任意失败写入 `failed` 并创建告警；beat 重新发现非终态构建时，从已存储的状态继续，不依赖 Celery result backend。

Jenkins job 必须输出 `orchestrator-result.json`：

```json
{
  "images": ["harbor.example/apps/example-api:build-123"],
  "compose": {
    "services": {
      "api": {"image": "harbor.example/apps/example-api:build-123"}
    }
  }
}
```

`images` 必须非空，最终 `compose.services` 必须非空且每个服务都包含镜像引用。产物缺失或格式无效时，worker 不部署未知镜像，任务失败并产生可查询告警。

## 6. 模板与基础镜像

静态目录覆盖 Python、Go、Java、C++、React、Vue、Next.js、HTML/CSS/JS、MongoDB、MySQL、PostgreSQL、Redis、MinIO、etcd。每个技术栈声明 2 至 3 个稳定版本镜像、端口、服务类型、默认环境变量及健康检查。

组合器校验组件存在、服务名冲突、调用关系和依赖环；它把调用关系转换成 Compose `depends_on`、网络别名和服务环境变量。组合后的文档可保存为 user-app 的构建输入。

Celery beat 定期投递基础镜像同步任务。同步 worker 从模板目录去重后遍历每个固定版本，使用 Docker Engine 拉取源镜像、标记为 `${HARBOR_REGISTRY}/boot-images/...` 并推送 Harbor；每个版本的摘要、时间、状态和错误写入 `base_images`，绝不混入 `user_images`。失败记录包含安全的错误摘要，并写入系统审计事件。

## 7. 外部适配器

- **GitLab**：使用项目 API 验证仓库和分支；仓库检出仍由 Jenkins job 完成。
- **Jenkins**：获取 crumb、触发参数化 build、解析 queue item、轮询 build、读取 JSON artifact。
- **Harbor/Docker Registry**：通过 Docker SDK 的 pull/tag/push 同步基础镜像；应用镜像由 Jenkins 生成后登记。
- **Docker Services**：通过 Docker SDK 的 Swarm Service API 创建或更新服务，使用 appid 命名空间隔离服务和网络。

适配器不得记录密码、token 或完整环境变量。网络错误转换为包含安全上下文的领域错误，并写入事件及告警。

## 8. 配置与部署

所有秘密由环境变量或 secret manager 注入，禁止将 `auth.txt` 内容提交到 Git。关键配置包括：

- `ORCHESTRATOR_STORAGE_BACKEND=mongo`
- `ORCHESTRATOR_MONGODB_URL`、`ORCHESTRATOR_MONGODB_DATABASE`
- `ORCHESTRATOR_CELERY_BROKER_URL=redis://redis:6379/0`
- `ORCHESTRATOR_DATA_ENCRYPTION_KEY`
- Jenkins、GitLab、Harbor、Docker Engine 的 URL 和认证项

`docker-compose.yml` 运行 API、worker、beat、MongoDB 和 Redis。MongoDB、Redis 与 beat 调度文件均使用持久卷；API、worker、beat 仅在 MongoDB 和 Redis 健康后启动。worker 在本地 Docker Engine 模式下才挂载 `/var/run/docker.sock`，远程 Engine 部署必须通过 override 移除该挂载并配置 TLS 端点。`.env.example` 仅保留占位符，真实凭据和 Fernet 密钥由部署环境或 secret manager 注入。

生产应使用外部受管 MongoDB/Redis、固定镜像 digest、TLS 与最小权限凭据。`ORCHESTRATOR_CELERY_RECOVERY_INTERVAL_SECONDS` 控制 non-terminal build 的恢复扫描间隔；较短的间隔会提高恢复速度，也会增加重复的幂等任务投递。

## 9. 验收标准

1. 无 JWT、错误 JWT 和错误 worker 凭据均被拒绝；有效 token 可访问所有业务接口。
2. Mongo 模式下重启 API 后 user-app、构建、事件、镜像、服务和告警仍可查询；appid 重复创建返回 409。
3. 创建构建会写入 `queued` 状态并投递 Redis broker；worker 可恢复并推进状态机。
4. fake GitLab/Jenkins/Harbor/Docker adapters 的集成测试覆盖成功、构建失败、Jenkins 等待、产物缺失和部署失败。
5. 模板覆盖所有指定技术栈，组合的 Python+MongoDB+React 和 Java+MySQL+Vue 均生成有效 Compose；未知依赖和循环依赖被拒绝。
6. 基础镜像同步任务为每个稳定版本写独立 `base_images` 记录，且不污染用户镜像集合。
7. Docker Compose 配置、静态编译和全量测试通过；每个功能点完成后提交包含变更和验证结果的详尽 commit message。
