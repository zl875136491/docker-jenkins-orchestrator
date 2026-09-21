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
| 模板列表/组合 | `GET /api/v1/docker/template_list`、`POST /api/v1/docker/template_compose` |
| 基础镜像列表/同步 | `GET /api/v1/docker/base_image_list`、`POST /api/v1/docker/base_image_sync` |

应用请求体仍使用领域字段 `appid`、`name`、`repository_url`、`git_ref`、`environment`、`compose` 和 `components`；路径参数统一使用 `app_id`。构建返回 `202` 和 `build_id`，不是同步完成结果。

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
