# API Reference

`GET /healthz` 不需要认证。`POST /oauth2/token` 和 `POST /oauth2/refresh` 用于认证；其余接口需要 `Authorization: Bearer <access_token>`。`/api/me` 已删除。

## OAuth2

```http
POST /oauth2/token
Content-Type: application/json

{"client_id":"local-worker","client_secret":"local-worker-secret"}
```

响应：

```json
{"access_token":"...","refresh_token":"...","token_type":"Bearer","expires_in":3600}
```

令牌过期后调用：

```http
POST /oauth2/refresh
Content-Type: application/json

{"refresh_token":"..."}
```

## 接口列表

| 用途 | 方法与路径 |
|---|---|
| 健康检查 | `GET /healthz` |
| 获取访问令牌 | `POST /oauth2/token` |
| 刷新访问令牌 | `POST /oauth2/refresh` |
| 系统调用说明 | `GET /api/v1/system-guide` |
| 完整 Markdown 使用指引 | `GET /api/v1/readme` |
| 创建应用 | `POST /api/v1/jenkins/app_create` |
| 应用列表 | `GET /api/v1/jenkins/app_list` |
| 应用详情/更新 | `GET/PATCH /api/v1/jenkins/app_info/{app_id}` |
| 同步 GitLab 仓库与 Compose | `POST /api/v1/jenkins/app_sync/{app_id}` |
| 创建构建 | `POST /api/v1/jenkins/build_create/{app_id}` |
| 构建历史 | `GET /api/v1/jenkins/build_list?app_id=<id>&page=1&page_size=20` |
| 构建详情 | `GET /api/v1/jenkins/build_info/{build_id}` |
| 事件、镜像、服务、入口、告警 | `GET /api/v1/jenkins/app_{events|images|services|access|alerts}/{app_id}` |
| 模板列表/组合 | `GET /api/v1/docker/template_list`、`POST /api/v1/docker/template_compose[?format=json|yaml]` |
| 技术栈 CRUD | `GET/POST /api/v1/docker/tech_stack_list`, `/api/v1/docker/tech_stack_create` |
| 技术栈详情/修改/删除 | `GET/PATCH/DELETE /api/v1/docker/tech_stack_info/{tech_stack_id}` |
| Compose 生成提示词 | `GET /api/v1/docker/compose_prompt` |
| 基础镜像列表/同步 | `GET /api/v1/docker/base_image_list`、`POST /api/v1/docker/base_image_sync` |

应用请求体仍使用领域字段 `appid`、`name`、`repository_url`、`git_ref`、`environment`、`compose` 和 `components`；路径参数统一使用 `app_id`。构建返回 `202` 和 `build_id`，不是同步完成结果。多 worker 部署时，每个 worker 在启动环境中使用 `WORKER_REGION` 标识所在区域，`app_access` 响应会在最外层返回 `region`。

### GitLab Compose 自动发现与仓库同步

配置 `ORCHESTRATOR_GITLAB_URL` 和 `ORCHESTRATOR_GITLAB_TOKEN` 后，创建应用时若没有提交 `compose`，服务端会先校验请求中的 `git_ref`（省略时为 `main`）并解析出固定 commit SHA，然后用 Git 对仓库执行完整克隆（不使用浅克隆、部分克隆或 GitLab tree/raw API），检出该 SHA 后在本地工作树中搜索 Compose 文件。运行镜像已安装 Git 和 CA 证书；克隆超时由 `ORCHESTRATOR_GIT_CLONE_TIMEOUT_SECONDS` 控制，默认 `600` 秒。候选文件名按 `compose.yaml`、`compose.yml`、`docker-compose.yml`、`docker-compose.yaml` 的主流/兼容命名规则识别；仓库根目录按该顺序优先选择，根目录没有候选时才接受唯一的嵌套候选，嵌套目录存在多个候选时返回歧义错误。文件必须是有效 YAML 对象并包含非空 `services`。如果创建请求显式提供 `compose`，则保留显式值，不从仓库覆盖。

扫描前会校验 Git 克隆不是 shallow/partial repository，并校验实际 checkout 的完整 SHA 与解析出的 commit 一致。Git 缺失、克隆失败、超时或检测到不完整克隆时返回 `502`，不会退回仓库树/文件 API，也不会把失败误报为“仓库没有 Compose”；同步失败不会改写已有 `app_info.compose` 或来源信息。凭据仅通过 Git 子进程配置传入，不写入克隆 URL 或日志，且认证的目标必须与已配置 GitLab 的 scheme/host/port 一致。

同步使用应用当前保存的仓库 URL 和 `git_ref`，无需请求体或 query 参数：

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/jenkins/app_sync/<app_id>
```

路径中的 `app_id` 是唯一业务入参；成功返回 HTTP `200`，响应体是同步结果。此操作只更新仓库同步元数据和 Compose，不会自动触发构建。

成功响应示例：

```json
{
  "appid": "demo-app",
  "git_ref": "main",
  "commit_id": "0123456789abcdef0123456789abcdef01234567",
  "previous_commit_id": "abcdef0123456789abcdef0123456789abcdef01",
  "commit_changed": true,
  "compose_file": "docker-compose.yml",
  "compose_changed": true,
  "compose": {"services": {"web": {"image": "example/web:1"}}},
  "updated_at": "2026-10-09T08:00:00Z"
}
```

同步会把发现的文档写入 `app_info.compose`，即后续构建使用的 Compose；`compose_changed` 比较新旧 Compose JSON，`commit_changed` 比较本次与此前记录的来源 commit。首次建立来源记录时 `previous_commit_id` 为 `null`，`commit_changed` 返回 `true`。同步成功后可通过 `GET /api/v1/jenkins/app_info/{app_id}` 查看最新 Compose 与来源元数据 `compose_source_file`、`compose_source_commit`。如果用户通过 `PATCH app_info` 修改 Compose、仓库地址或分支，来源元数据会清空，下一次同步仍会按 GitLab 结果更新。

请在构建前检查仓库 Compose 是否依赖本机 `env_file`、bind mount、`build` context、固定宿主机资源或环境专属密钥；自动发现只负责识别和导入，不会猜测或改写项目专属部署参数。错误状态包括：`404` 应用不存在、`422` 分支或 Compose 文件缺失/歧义/格式错误、`502` GitLab 请求失败、`503` 未配置 GitLab。

### 技术栈记录

技术栈记录的三个核心字段必须保持一致：

```json
{
  "tech_stack_id": "react",
  "name": "React",
  "yaml_original": "# React 技术栈配置\nimages:\n  # 固定版本镜像说明\n  # 固定基础镜像版本\n  - node:20.18.1-alpine3.20\n# 确认应用实际监听端口\nport: 3000\n",
  "json_data": {"images": ["node:20.18.1-alpine3.20"], "port": 3000},
  "line_comments": {
    "$": "React 技术栈配置",
    "$.images": "固定版本镜像说明",
    "$.images[0]": "固定基础镜像版本",
    "$.port": "确认应用实际监听端口"
  }
}
```

`yaml_original` 返回带逐路径注释的 YAML，解析后的对象必须与 `json_data` 深度相等。创建和修改请求可以提交无注释 YAML；服务端会依据 `line_comments` 重新生成带注释的 YAML。注释使用 JSONPath-like 路径，缺失或空白注释会补充该技术栈的适配说明，非空自定义注释会保留，不存在于 `json_data` 的路径会被拒绝。JSON 仍必须符合平台 Compose 组件结构（`images`、`port` 等），这样新增数据会立即参与 `template_compose`。

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/docker/tech_stack_list
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  http://localhost:8000/api/v1/docker/tech_stack_create -d @tech-stack.json
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/docker/compose_prompt
```

## 最短调用示例

```bash
TOKEN_JSON=$(curl -sS http://localhost:8000/oauth2/token \
  -H 'content-type: application/json' \
  -d '{"client_id":"local-worker","client_secret":"local-worker-secret"}')
TOKEN=$(printf '%s' "$TOKEN_JSON" | jq -r .access_token)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/jenkins/app_list
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/system-guide
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/readme
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/jenkins/build_info/<build_id>
```

常见错误：`401` 为客户端凭据或 Bearer 令牌无效，`404` 为应用或构建不存在，`409` 为 appid 重复，`422` 为请求或 Compose 无效。

### Compose 组合输出格式

`POST /api/v1/docker/template_compose` 的请求体仍为：

```json
{"components": ["react", "mongodb"], "dependencies": {"react": ["mongodb"]}}
```

可通过 query 参数选择交付材料：

- 不传 `format`：返回历史兼容的 Compose JSON 文档。
- `format=json`：返回 `{ "format": "json", "json_data": <Compose JSON>, "line_comments": <JSONPath 注释> }`。
- `format=yaml`：返回 `application/yaml`，内容为带 JSONPath 语义注释的 YAML；注释不会改变 YAML 解析结果。

Compose 中的宿主机 published 端口只是用户申请值。部署 worker 会结合现有 Swarm 服务检查冲突并在配置范围内重新分配，最终 `published_port` 可能不同；容器侧 target 端口不会被替换。`expose` 只用于内部网络，不会生成外部访问入口。

## 应用访问入口

主程序消费 `GET /api/v1/jenkins/app_access/{app_id}` 时，应以应用级 `access_available` 和 `access_urls` 决定是否展示入口；服务级无入口对数据库、缓存、搜索等内部依赖是正常状态。完整字段契约、Linkwarden 多服务示例和 TypeScript 调用方式见 [应用访问入口 API](app-access.md)。
