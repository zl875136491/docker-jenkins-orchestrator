# 运行时凭据推送约定（APPS × Orchestrator）

> 对照：*APPS × Jenkins 运行时凭据注入接口清单 v1.2*  
> 结论：**本期不接入 `POST /secrets/runtime/resolve`**。密钥由 APPS 经现有应用接口推送，Worker 在校验/部署时解密展开并注入 Swarm；Jenkins 构建参数中的 `environment` 恒为空。

## 职责边界

| 环节 | 负责方 | 说明 |
|------|--------|------|
| 开通/变更密钥 | **APPS** | `POST app_create` 或 `PATCH apps/{appid}` 写入 `environment` |
| 触发构建 | **APPS** | `POST apps/{appid}/builds`，body 可为 `{}` 或仅 `git_ref` |
| 镜像构建与推送 | **Jenkins Job** | 只构建推镜像；**不**领取密钥、**不**做 compose up |
| 校验、解密、注入、部署、探针 | **Orchestrator Celery Worker** | 部署与注入的唯一执行点 |
| 构建结束后的密钥收敛 | **APPS（约定）** | 可选：终态后 PATCH 清空或轮换 `environment` |

## 联调流程

1. **写入本轮密钥** — APPS 推送 `environment` 键值对。编排器使用 Fernet（SecretBox）加密落库；读接口仅返回 `environment_keys`，不回显明文。
2. **触发构建** — `build_create` 可为空对象。VALIDATING 阶段解密并展开 Compose 中的 `${VAR}` / `${VAR:?…}`；缺必填变量则在触发 Jenkins **之前**失败。
3. **Jenkins** — Worker 触发 Job 时固定 `environment={}`（`ENVIRONMENT_JSON` 为 `{}`）。Compose 中保留占位符，不把真值写入 Job 参数。
4. **部署注入** — Jenkins 产物就绪后，Worker 再次解密 → 展开 → Swarm 注入 → 端口/动态路由检查 → `succeeded`。
5. **（可选）收敛** — 构建进入 `succeeded` / `failed` / `cancelled` 且确认无进行中构建后，APPS 可 PATCH 清空或轮换 `environment`；已运行的服务不受影响。

## Compose 变量

服务 `environment` 支持 `$NAME`、`${NAME}`、`${NAME:-default}`、`${NAME-default}`、`${NAME:?hint}`、`${NAME?hint}`。  
缺少或空值（`:?`）时，错误信息包含变量名与 Compose 中编写的 hint，**不会**回显已解密的密钥值。

## 约定与限制

1. **不调用 resolve** — 编排器不配置、不发起 APPS resolve 拉取。
2. **并发** — 同一 `appid` 上并行多构建并互相 PATCH `environment` 不安全；请串行或拆 app。
3. **多环境** — 现模型为一 app 一份扁平 `environment`；多目标用多 `appid` 或串行 PATCH，本期不引入 `formal/test_*`。
4. **字段风格** — 继续使用 `appid` / `build_id` 等既有路径字段。

## 相关代码锚点

- 写入/加密：`orchestrator/services.py`（`create_app` / `update_app`）+ `orchestrator/secrets.py`
- 读 API 仅 keys：`UserApp.environment_keys`
- VALIDATING 展开 + Jenkins `environment={}`：`orchestrator/worker_runtime.py`（`_start_build` / `_resolve_application_environment`）
- DEPLOYING 注入：`_complete_deployment`

## 验收要点

- [ ] 仅占位符 Compose + 推送完整 `environment` → 构建成功且容器内变量正确
- [ ] 缺必填变量 → validating/failed，Jenkins 未被误触发
- [ ] Jenkins 参数 / 日志 / 镜像层无密钥明文；`ENVIRONMENT_JSON={}`
- [ ] `app_info` 不回显 environment 值
- [ ] 成功后 PATCH 清空 `environment`，运行中服务仍可用
