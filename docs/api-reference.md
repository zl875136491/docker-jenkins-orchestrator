# API Reference

除 `GET /healthz` 与 `POST /api/connect` 外，所有接口都需要 `Authorization: Bearer <JWT>`。

| 用途 | 方法与路径 |
|---|---|
| 健康检查 | `GET /healthz` |
| 登录、获取 JWT | `POST /api/connect` |
| 当前身份 | `GET /api/me` |
| 系统调用说明 | `GET /api/system-guide` |
| 应用列表/创建 | `GET/POST /api/apps` |
| 应用详情/更新 | `GET/PATCH /api/apps/{appid}` |
| 创建构建 | `POST /api/apps/{appid}/builds` |
| 构建历史 | `GET /api/builds?appid=<id>&page=1&page_size=20` |
| 构建详情 | `GET /api/builds/{build_id}` |
| 事件、镜像、服务、入口、告警 | `GET /api/apps/{appid}/{events|images|services|access|alerts}` |
| 模板 | `GET /api/templates`、`POST /api/templates/compose` |
| 基础镜像 | `GET /api/base-images`、`POST /api/base-images/sync` |

典型请求：

```bash
TOKEN=$(curl -s localhost:8000/api/connect -H 'content-type: application/json' \
  -d '{"worker_name":"...","worker_secret":"..."}' | jq -r .access_token)
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/system-guide
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/builds/<build_id>
```

创建构建返回 `202` 和 `build_id`，不是同步完成结果。常见错误为 `401`（JWT 无效）、`404`（应用或构建不存在）、`409`（appid 重复）和 `422`（Compose 无效）。
