# 10.32.12.111 部署记录

目标目录：`/opt/orchestrator`。部署使用独立的 Docker Compose 项目 `orchestrator-route-checks`，不修改主机上已有容器、MongoDB、端口 8000 或 Swarm 全局状态。

## 主机与依赖

2026-10-21 核验目标主机为 Ubuntu 25.10、x86_64，Docker Engine 29.7.2、Docker Compose v5.4.0 已安装且 Docker 服务运行中；主机有约 30 GiB 内存和 825 GiB 可用磁盘。Compose 会提供 FastAPI、Celery worker、Celery beat、MongoDB 8 和 Redis。Redis 只作任务 broker，MongoDB 保存业务数据；MongoDB 与 Redis 不发布主机端口。

主机已有其他运行中的容器，TCP 8000 已被占用，因此新控制台使用 `10.32.12.111:8088`。工作区数据置于 `/opt/orchestrator/data/workspaces`，MongoDB、Redis、Beat 和 Codex 登录数据使用项目专属 Docker volumes。凭据只放在权限为 `0600` 的 `/opt/orchestrator/.env`；此文件不参与代码同步、不进入镜像或版本库。

应用镜像已包含 Git、CA 证书、Python 依赖，以及固定版本 Codex CLI 和 Node.js 运行时，不需要在宿主机额外安装这些组件。2026-10-21 实测运行版本如下（实际下载的依赖版本，以当前镜像为准）：

| 组件 | 运行版本 / 用途 |
| --- | --- |
| Python / FastAPI / Uvicorn | 3.12.14 / 0.156.5 / 0.54.0，API 与管理页面 |
| Celery | 5.6.3，Worker 执行任务，Beat 定时调度 |
| Redis 服务端 / Python 客户端 | 7.4.11 / 6.4.0，持久化任务 broker，不作为业务结果后端 |
| MongoDB / PyMongo | 8.3.13 / 4.19.0，业务状态与检查结果 |
| Docker SDK | 7.2.0，Worker 通过 Unix socket 访问主机 Engine |
| Codex CLI / Node.js / Git | 0.147.0 / 22.23.3 / 2.47.3，仓库静态检查运行环境 |
| gRPC / Reflection / Protobuf | 1.84.0 / 1.84.0 / 7.36.2，动态路由探针 |

## 当前验收结果

- API、Worker、Beat、MongoDB 和 Redis 五个容器均为 `healthy`；API 仅发布 `10.32.12.111:8088`。
- 从部署主机和当前工作机访问 `/healthz` 返回 `200 {"status":"ok"}`，`/ui/` 返回 `200`。
- 20 项 HTTP 冒烟检查通过：页面及资源、OpenAPI 新接口、登录/刷新、缺失与无效 token、refresh token 权限隔离、两个默认 Agent Role、静态/动态请求参数校验；没有创建应用或提交构建。
- Celery `inspect ping` 返回 `pong`，已注册静态检查、动态检查及恢复任务；日志确认 Worker 已连接 Redis、Beat 已启动。
- 镜像 `pip check` 通过；Worker 内 Codex CLI、Git 和只读 Docker `ping/info` 均通过。宿主机 Swarm 保持 `inactive`，本次未执行真实构建、模型调用或 Swarm 服务部署。

前端已支持应用的 `require_route` 配置、手动静态检查、按完整 commit SHA 查询报告、按 Build ID 动态重验，以及角色版本和检查证据展示。

## 启用真实检查和交付前还需配置

- GitLab URL 和 Token：静态检查需要访问关联仓库并 checkout 固定 commit。
- `OPENAI_API_KEY`，或在 Worker 的 `codex-data` volume 中完成 Codex CLI 登录。
- Jenkins URL、用户、密码和 Job；Harbor URL、用户和密码。
- Docker Engine socket。该 Worker 可控制主机 Docker；只应运行可信仓库。
- 当前 Docker Engine 的 Swarm 状态是 `inactive`。真正部署用户服务及完成动态检查前，需要专用且经授权的 Swarm Engine。因为主机已有其他容器，此部署不自行执行 `docker swarm init`，避免改变共享 Engine 状态。

- GitLab URL/Token、Jenkins 地址/用户/密码和 Harbor 地址/用户/密码当前未配置；GitLab checkout、镜像发布及真实 Jenkins 交付还不能工作。
- Worker 环境中存在非空 `OPENAI_API_KEY`，但未发起模型请求验证密钥有效性或配额；Codex CLI volume 当前没有登录。请完成实际凭据验证后再触发静态检查。
- Worker 挂载 Docker socket 并以 root 运行 Celery（日志会显示 Celery 超级用户警告），这赋予其接近宿主机 root 的 Docker 权限。仅允许可信仓库访问此控制面，并限制 8088 的网络来源；不要将服务直接暴露到不可信网络。
- 当前 Docker Engine 的 Swarm 为 `inactive`；真实服务部署及动态探针还需要单独确认并授权该主机的 Swarm 用途。本次没有运行真实仓库检查、模型请求或 Swarm 服务部署。

在补齐缺失的第三方集成之前，控制台、角色配置以及任务/数据库基础服务仍可用；真实仓库检查和交付不能完成，门禁会失败关闭，不会伪装通过。Worker 当前默认 concurrency 为 16；若开放真实 Agent 任务前需要限流，可在 `.env` 设置对应 Celery Worker 并发配置并重新创建 Worker。

## 操作与验收

此主机访问 `deb.debian.org` 超时，应用镜像构建通过环境变量 `ORCHESTRATOR_DEBIAN_MIRROR=https://mirrors.aliyun.com` 选择可达镜像源（也可将这一非敏感设置写入 `.env`）。该选项仅影响镜像内 Debian 软件包下载，不修改宿主机源、不进入运行时配置，APT 仍验证 Debian 包签名。默认构建源仍为 `https://deb.debian.org`；`apt-get` 配有有限重试和连接超时。

在 `/opt/orchestrator` 供应已填好的 `.env` 后执行：

```sh
cd /opt/orchestrator
docker compose config -q
ORCHESTRATOR_DEBIAN_MIRROR=https://mirrors.aliyun.com docker compose build api worker beat
docker compose up -d --no-build
docker compose ps
docker compose logs --tail=100 api worker beat
curl --fail http://10.32.12.111:8088/healthz
```

UI：`http://10.32.12.111:8088/ui/`。在连接配置中选择当前服务地址，Client ID 使用 `.env` 中的 `ORCHESTRATOR_WORKER_NAME`，Client Secret 使用 `ORCHESTRATOR_WORKER_SECRET`；页面预填的本地开发凭据不适用于此环境。首次部署已生成唯一随机凭据，请通过免密 SSH 在主机上安全读取，不要在日志或聊天中输出。当前使用内网 HTTP，跨不可信网络访问前应加 TLS 反向代理。
