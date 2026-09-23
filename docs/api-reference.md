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
| 创建构建 | `POST /api/v1/jenkins/build_create/{app_id}` |
| 构建历史 | `GET /api/v1/jenkins/build_list?app_id=<id>&page=1&page_size=20` |
| 构建详情 | `GET /api/v1/jenkins/build_info/{build_id}` |
| 事件、镜像、服务、入口、告警 | `GET /api/v1/jenkins/app_{events|images|services|access|alerts}/{app_id}` |
| 模板列表/组合 | `GET /api/v1/docker/template_list`、`POST /api/v1/docker/template_compose[?format=json|yaml]` |
| 技术栈 CRUD | `GET/POST /api/v1/docker/tech_stack_list`, `/api/v1/docker/tech_stack_create` |
| 技术栈详情/修改/删除 | `GET/PATCH/DELETE /api/v1/docker/tech_stack_info/{tech_stack_id}` |
| Compose 生成提示词 | `GET /api/v1/docker/compose_prompt` |
| 基础镜像列表/同步 | `GET /api/v1/docker/base_image_list`、`POST /api/v1/docker/base_image_sync` |

应用请求体仍使用领域字段 `appid`、`name`、`repository_url`、`git_ref`、`environment`、`compose` 和 `components`；路径参数统一使用 `app_id`。构建返回 `202` 和 `build_id`，不是同步完成结果。

### 技术栈记录

技术栈记录的三个核心字段必须保持一致：

```json
{
  "tech_stack_id": "react",
  "name": "React",
  "yaml_original": "images:\n  - node:20.18.1-alpine3.20\nport: 3000\n",
  "json_data": {"images": ["node:20.18.1-alpine3.20"], "port": 3000},
  "line_comments": {
    "$": "",
    "$.images": "",
    "$.images[0]": "固定基础镜像版本",
    "$.port": "确认应用实际监听端口"
  }
}
```

`yaml_original` 解析后的对象必须与 `json_data` 深度相等。`line_comments` 使用 JSONPath-like 路径，服务端会为缺少的路径补 `""`，并拒绝不存在于 `json_data` 的路径。创建和修改技术栈时，JSON 仍必须符合平台 Compose 组件结构（`images`、`port` 等），这样新增数据会立即参与 `template_compose`。

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
