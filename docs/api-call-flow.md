# API 调用流程

## 标准顺序

1. `POST /oauth2/token` 使用 `client_id` 和 `client_secret` 获取 `access_token`、`refresh_token`。
2. `GET /api/v1/jenkins/app_list` 选择应用，或 `POST /api/v1/jenkins/app_create` 创建应用。
3. 通过 `GET /api/v1/docker/tech_stack_list` 读取平台模板；必要时用技术栈 CRUD 更新模板，或调用 `GET /api/v1/docker/compose_prompt` 获取给用户模型的 Markdown 提示词。
4. 使用 `POST /api/v1/docker/template_compose?format=json` 获取 Compose JSON 与完整路径注释，或使用 `?format=yaml` 获取带注释 YAML；也可以提交用户模型输出的 `yaml_original`、`json_data`、`line_comments` 后再生成。
5. `PATCH /api/v1/jenkins/app_info/{app_id}` 保存包含 `services` 的 Compose。
6. `POST /api/v1/jenkins/build_create/{app_id}` 创建异步构建并保存返回的 `build_id`。
7. 每 3--10 秒调用 `GET /api/v1/jenkins/build_info/{build_id}`，直到状态为 `succeeded`、`failed` 或 `cancelled`。
8. 成功后查询 `app_images`、`app_services`、`app_access`；失败后查询 `app_events` 和 `app_alerts`。

访问令牌过期时，调用 `POST /oauth2/refresh`，然后用新 access token 重试业务请求。`/api/me` 不再提供。

## 技术栈与用户模型协作

技术栈不是只有一个名称列表，而是可持久化的三维记录：带注释的 YAML、解析后的 JSON 和 JSONPath 对齐的行间注释。注释用于告诉内部模型哪些镜像、端口、变量、卷和依赖需要结合项目实际情况修改；服务端生成或更新 YAML 时会把这些说明写进对应路径上方，同时保证解析后的数据不变。平台保存记录后，技术栈会立即进入 Compose 组合器；删除技术栈会使后续组合请求拒绝该组件。

## 角色

WebUI 调用 Control API；MongoDB 保存应用、构建、事件、镜像、服务和告警；Redis 仅作为 Celery broker；Worker 调用 Jenkins；Jenkins checkout Git、构建并推送 Harbor；Worker 最后部署 Docker Swarm。

## Compose 规则

构建前必须保存有效的 `services` mapping。系统不会自动从 Git 查找 Compose。`ports` 才会申请外部入口，例如 `18082:3000` 表示期望宿主机 18082 转发到容器 3000；worker 会在部署前检查冲突并可能重新分配宿主机 published 端口，实际端口以 `app_access`/`published_ports` 为准。`expose` 仅供容器网络使用。服务必须真正监听 target port。

## 排障顺序

按 WebUI 请求、Control API、MongoDB、Celery/Redis、Jenkins、Harbor、Swarm、外部 HTTP 依次检查。构建成功但没有入口时，对照源 Compose、Jenkins artifact、服务记录的 `published_ports` 与 Swarm `Endpoint.Ports`。

机器可读说明：`GET /api/v1/system-guide`；完整 Markdown：`GET /api/v1/readme`。
