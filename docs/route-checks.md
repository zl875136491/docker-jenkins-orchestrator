# 路由检查、Agent 自愈与交付门禁

静态检查复用 Celery/Redis 调度和 MongoDB 仓储，不构建镜像、不推送代码。
只有精确通过静态检查的版本才能进入 Jenkins。容器启动也不等于交付成功：
目标 HTTP/gRPC 路由的动态检查通过后才标记 `succeeded`。

## 部署与安全边界

Worker 需要本地 `codex` 可执行文件和有效认证。Dockerfile 安装固定版本
`@openai/codex@0.147.0`，可用 `CODEX_CLI_VERSION` build argument 显式更换。
直接运行 Worker 时可以自行安装，并用 `ORCHESTRATOR_CODEX_EXECUTABLE` 指定路径。
Python 直接启动进程，不经过 shell，上下文经 stdin 传入。
执行器使用 `--ephemeral` 避免把包含源码或容器日志的检查会话持久化到登录卷；
stdout/stderr 不进入应用日志，仅读取有大小限制的最终结构化结果。

Compose 只向 Worker 传入 `OPENAI_API_KEY`，不向 API/Beat 传入，不把凭据写进镜像。
也可用 Worker 的 `codex-data:/root/.codex` 卷保存已授权的 CLI 登录信息。
不要提交真实 API key、`auth.json` 或运行日志。
Compose 中为空的 `OPENAI_API_KEY` 不传给子进程，保留使用卷内已有登录的路径。
镜像只复制明确列出的运行时代码、模板、文档及前端，不复制项目根目录临时认证文件。

启用前先在相同 Worker 中检查 `codex --version` 和 `codex login status`。
需要交互登录时可用 `docker compose run --rm --no-deps worker codex login --device-auth`
将授权保存在同一个命名卷；使用 API key 时通过部署环境注入 `OPENAI_API_KEY`。
上述检查不代表真实模型调用已完成，仍需在有权限且确认费用后做端到端验证。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `ORCHESTRATOR_CODEX_EXECUTABLE` | `codex` | Worker 内的本地 CLI |
| `ORCHESTRATOR_ROUTE_CHECK_LEASE_SECONDS` | `120` | 静态执行租约，执行期间自动续期 |
| `ORCHESTRATOR_ROUTE_PROBE_TIMEOUT_SECONDS` | `15` | 单个探针超时 |
| `ORCHESTRATOR_ROUTE_PROBE_MAX_RESPONSE_BYTES` | `65536` | 响应上限，包括解压后的 HTTP 响应 |
| `ORCHESTRATOR_PUBLIC_HOST` | 未设置 | 从 Worker 可达的 Swarm 发布端口主机 |
| `ORCHESTRATOR_PUBLIC_SCHEME` | `http` | HTTP 或 HTTPS；gRPC 对应明文或 TLS |

默认角色按需求使用 `authorization_mode=never`（CLI 的 `approval_policy`）、`sandbox_mode=danger-full-access`、
`skip_git_repo_check=true`。这是高权限自动化，不是安全沙箱。只在隔离、受控的私有
Worker 上执行信任的仓库；不要把多租户不可信代码与宿主秘密或 Docker socket 混用。
可通过角色 API 选择 `read-only` / `workspace-write` 及其他授权策略。
静态角色在临时 checkout 中生成补丁时需要写权限。

角色的业务输出 Schema 保持不变。为了兼容 CLI 的严格结构化输出子集，执行器向 CLI
提供固定的 `{"response_json":"<业务 JSON 字符串>"}` 传输结构，并在本地解码后按角色
原始 Schema 校验；`probe_payload`、`retry_payload` 在持久化与 API 中仍然是对象或 null。

Agent 不决定探针地址。服务身份、主机及端口来自当前构建的资源清单和部署记录。
优先使用 public host + 实际 published port；否则使用构建专有 Swarm DNS + target/expose
port，要求 Worker 接入可达的 overlay 网络。没有可达目标时明确失败，不跳过门禁。
HTTP 不跟随跳转。

## App 和静态检查

沿用 `POST /api/v1/jenkins/app_create`、`PATCH /api/v1/jenkins/app_info/{app_id}`
和 `POST /api/v1/jenkins/app_sync/{app_id}`，设置关联 Git 仓库及单个绝对路径
`require_route`，例如 `/health`。创建、元信息更新、仓库同步都会解析目标分支
最新完整 commit SHA，按当前路由创建或复用静态检查。
历史 App 可不配置路由继续查询和修改，但不能创建新构建。

每次静态检查冻结仓库、commit、路由和角色快照，在独立临时目录完整克隆并 checkout
精确 SHA。不会提交、合并或推送 Agent 生成的补丁。

- 路由存在且未产生修改：验证 `probe_payload` 后通过。
- 可以小幅修补：保存真实 Git Diff（含新增文件），检查仍失败；调用端审核提交后重跑。
- 无法修补或执行失败：保存精简 `static_report_text`，禁止构建。
- Agent 改动了文件或 HEAD，即使声称路由存在，也不能把原 commit 标记为通过。

缓存键严格为 `(app_id, commit_id, require_route, static_agent_role_version)`。
成功缓存返回 `命中已成功的历史缓存, 成功并跳过`；失败缓存返回
`命中已失败的检查结果缓存, 未执行静态检查`。排队/执行中只复用任务，不伪装成完成缓存。

构建创建和 Worker 触发 Jenkins 前都校验四维门禁。角色改版、路由变更、缓存删除、
force 导致 generation 变化都会使旧排队构建失效。拒绝文本固定为：

```text
未进行路由静态检查, 无法交付构建任务
```

## HTTP/gRPC 探针

HTTP 示例（path 可省略；如提供必须等于 `require_route`）：

```json
{
  "protocol": "http", "service_name": "web", "target_port": 8080,
  "path": "/health", "method": "GET", "query_params": {}, "headers": {}, "body": null,
  "assertions": {"expected_status_list": [200], "json_contains": {"status": "ok"}}
}
```

可选断言：`expected_headers`、`body_equals`、`body_contains`、`json_equals`、
`json_contains`、`json_path_equals`。状态列表必须非空；所有附加断言也必须通过。
禁止任意 URL、host、网络目的地址以及 Host/传输 framing 请求头。

gRPC 使用 `/package.Service/Method` 形式的路由：

```json
{
  "protocol": "grpc", "service_name": "rpc", "target_port": 50051,
  "body": {"message": "hello"},
  "assertions": {"expected_grpc_status_list": ["OK"]}
}
```

默认用有超时和尺寸限制的服务反射解析 JSON body。未启用反射时可提供
`descriptor_set_base64`（FileDescriptorSet），或用 `body_base64` 发送原始 protobuf。
仅支持一元调用，不支持 streaming。gRPC 状态也接受 0–16 整数。

## 单次自愈、重启恢复和资源清理

```text
首次探针 → 通过 → 保存最终响应 → 交付成功
   ↓ 失败并保存首次证据
当前 build 日志 → Debug Agent 诊断 / 构造复杂请求
   ↓ 最多一次重试，目标及原断言不变
通过 → 保存最终响应 → 交付成功
失败 → 保存诊断 → 构建 Failed → 释放执行租约 → 立即 Teardown
```

首次探针、Agent 调用和重试开始前保存预约标记。Worker 崩溃后不重复不确定的外部请求、
不额外消耗重试预算；中断会明确失败，不会声称恢复成功。
Debug Agent 使用构建冻结的角色版本，不允许修改 Docker、源代码、网络或数据库。

日志只读取 native ID 和 app/build/namespace 所有权全部匹配的 Swarm 服务。
每服务最多 100 行，总计最多 200 行 / 64 KiB，已知请求凭据和环境中的秘密进行脱敏。
`probe_result.initial` 保存首次证据，`retry` 保存唯一重试，`final` 保存最终响应；
失败还可查看 `container_logs`、`dynamic_report_text`。构建和检查详情均可查询。

Teardown 使用 build_id 专属清理器，清理服务、容器、私有网络、卷、workspace、运行目录、
端口及专有镜像，保留 App 和构建历史，不影响同 App 的其他构建。
自动 teardown 不依赖 broker，释放部署租约后立即执行。
不可验证或不可达的资源保持 `teardown_required=true`、`teardown_status=partial_failed`，
由 Beat 恢复；不把部分失败报告为成功。Harbor blob 的物理释放仍取决于 GC。

## API

全部使用现有 Bearer Token。commit 参数必须是完整 40 或 64 位十六进制 SHA。

| 方法与路径 | 行为 |
| --- | --- |
| `GET /api/v1/agent-roles` | 两个内置角色列表 |
| `GET /api/v1/agent-roles/{role_key}` | 当前 Prompt、Schema、参数和版本 |
| `PUT /api/v1/agent-roles/{role_key}` | 每次配置更新生效后版本 +1 |
| `POST /api/v1/apps/{app_id}/static-checks` | 强制重跑；body 可省略或为 `{"commit_id":"完整SHA"}`；202 |
| `GET /api/v1/apps/{app_id}/commits/{commit_id}/check-results` | 此 commit 所有历史路由/版本维度列表 |
| `DELETE /api/v1/apps/{app_id}/commits/{commit_id}/check-results` | 仅删除当前路由 + 最新静态版本；返回 JSON bool |
| `POST /api/v1/apps/{app_id}/dynamic-checks` | body `{"build_id":"构建ID"}`；重验运行中部署；202 |

手动重验成功构建会暂时回到 `deploying`；新检查通过才再次成功，失败则标记 `failed`
并回收资源。资源操作/清理中、未部署、其他 App 的 build 或已清理 build 均不能重验。
已有排队/执行中的动态 run 不再额外创建。

强制静态重跑以 generation 区分旧消息。每个构建保留自己的动态证据；共用静态缓存时，
检查列表展示最近领取的动态 run，历史从各自 build_info 查询。
检查响应不返回内部执行 token、仓库凭据或完整角色快照。

错误码：401 缺失/无效 Token，404 App/角色/构建不存在，409 并发冲突，
422 输入或门禁不满足，503 无法投递。投递失败仍保存排队任务，Beat 可恢复。

角色更新示例（未指定的 runtime 字段保持不变；模型和思考程度未指定时沿用 CLI 默认）：

```json
{"runtime_config": {"authorization_mode": "never", "sandbox_mode": "danger-full-access", "skip_git_repo_check": true, "timeout_seconds": 900}}
```

## 验证

运行 `pytest -q`。模块测试覆盖执行器、角色/仓储、静态检查、门禁、探针、日志、
动态状态、API 和 Worker。集成使用临时真实 Git、本地 HTTP/gRPC 服务和 CLI 子进程；
本地 MongoDB/Redis 测试使用隔离数据目录和端口。Jenkins、Harbor、Docker 适配器在自动
测试中使用可控替身，不能将其描述为真实外部生产交付验证。
