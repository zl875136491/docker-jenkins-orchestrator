# API 调用流程

## 角色

WebUI 调用 Control API；MongoDB 保存应用、构建、事件、镜像、服务和告警；Redis 仅作为 Celery broker；Worker 调用 Jenkins；Jenkins checkout Git、构建并推送 Harbor；Worker 最后部署 Docker Swarm。

## 标准顺序

1. `POST /api/connect` 获取 JWT。
2. `GET /api/apps` 选择应用，或 `POST /api/apps` 创建应用。
3. `PATCH /api/apps/{appid}` 保存包含 `services` 的 Compose。
4. `POST /api/apps/{appid}/builds` 创建异步构建并保存返回的 `build_id`。
5. 每 3--10 秒调用 `GET /api/builds/{build_id}`，直到状态为 `succeeded`、`failed` 或 `cancelled`。
6. 成功后查询 `images`、`services`、`access`；失败后查询 `events` 和 `alerts`。

构建状态依次可能为 `queued`、`validating`、`triggering`、`building`、`deploying`，之后进入终态。Worker 会校验 Compose，调用 Jenkins，读取 artifact，校验端口和服务拓扑，登记镜像并部署 Swarm。

## Compose 规则

构建前必须保存有效的 `services` mapping。系统不会自动从 Git 查找 Compose。`ports` 才会产生外部入口，例如 `18082:3000` 表示宿主机 18082 转发到容器 3000；`expose` 仅供容器网络使用。服务必须真正监听 target port。

## 排障顺序

按 WebUI 请求、Control API、MongoDB、Celery/Redis、Jenkins、Harbor、Swarm、外部 HTTP 依次检查。构建成功但没有入口时，对照源 Compose、Jenkins artifact、服务记录的 `published_ports` 与 Swarm `Endpoint.Ports`。

机器可读的同一份说明可通过认证接口 `GET /api/system-guide` 获取。
