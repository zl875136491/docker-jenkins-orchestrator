# 简单前端：仓库 Compose 与同步

打开测试环境 `/ui/`，在“连接设置”中连接控制端，再进入“应用配置”。前端复用后端 API，不自行解析仓库或另建同步协议。

## 创建应用

填写 App ID、应用名称、GitLab 仓库和 Git 引用。Git 引用留空时使用 `main`；仓库使用 `master` 时需要明确填写 `master`。

- Docker Compose JSON 留空：创建请求省略 `compose`，由已配置 GitLab 的后端完整克隆仓库、检出固定 commit，再从本地工作树发现 Compose。不会使用浅克隆、部分克隆或仓库树 API 替代完整克隆。
- 填写 Docker Compose JSON：使用显式提交的文档，不被仓库文件覆盖。`{}` 不属于“留空”，不会触发自动导入。
- 根目录按 `compose.yaml`、`compose.yml`、`docker-compose.yml`、`docker-compose.yaml` 顺序选择；根目录没有候选时接受唯一的嵌套候选，多个嵌套候选需要用户明确整理或提交 Compose。

成功后页面显示保存的 Compose、来源文件和完整 commit。手工配置的 Compose 没有仓库来源记录；未保存 Compose 的应用会显示未配置状态。

## 同步已有应用

先“读取应用”或在“当前应用”中选择已有应用，确认保存的仓库和分支。点击“同步仓库 Compose”并确认覆盖提示。

```http
POST /api/v1/jenkins/app_sync/{app_id}
Authorization: Bearer <access_token>
```

请求没有 body，也不发送页面上的仓库、分支、环境变量或 Compose 草稿。后端使用应用已经保存的 `repository_url` 和 `git_ref`。如要切换仓库或分支，应先保存应用配置，再执行同步。

同步会替换后端保存的 Compose；页面读取 `app_info` 后更新 Compose 编辑框和来源信息。操作不会自动触发构建。需要部署新配置时，在“构建任务”中单独提交构建。

结果展示以下字段，同时保留原始 JSON 供核对：

| 字段 | 页面含义 |
|---|---|
| `git_ref` | 实际同步的已保存分支或 Git 引用 |
| `commit_id` | 此次同步的完整 commit |
| `previous_commit_id` | 上次记录的来源 commit；首次同步可能为空 |
| `compose_file` | 本次导入的仓库文件路径 |
| `compose_changed` | 与原先保存的 Compose JSON 是否不同 |
| `commit_changed` | 与上次记录的来源 commit 是否不同 |
| `updated_at` | 此次同步更新时间 |

同一个 commit 再次同步时，两个标志可以均为 `false`。仅源码变化但 Compose 内容相同时，`commit_changed` 可以为 `true` 而 `compose_changed` 为 `false`。

## 等待与失败处理

完整克隆可能耗时。等待期间页面提示同步进行中并防止重复提交；后端克隆超时由 `ORCHESTRATOR_GIT_CLONE_TIMEOUT_SECONDS` 控制，默认 600 秒。

同步失败时显示后端错误，并保留已有编辑内容和来源信息。如果同步 POST 成功但随后的详情 GET 失败，页面会区分“同步成功、刷新失败”，保留同步结果；使用“读取应用”重新取得已保存配置，不需要因此再次同步。在当前应用成功刷新前，页面会阻止提交构建，避免用旧编辑器内容误判服务器保存的配置。

读取接口只返回环境变量键名，不返回秘密值。更新应用时，未编辑的环境变量不发送，避免清空后端秘密；有意编辑环境变量时提交的 JSON 会替换完整环境配置。未修改的仓库、分支和 Compose 也不重复发送，避免无关更新清空来源记录。

构建区域的 commit 来自构建详情中的 `git_commit_sha`，应以此确认实际构建版本，不能把上一次同步的来源 commit 当成构建已经使用的版本。

“访问入口”显示 `app_access.region` 返回的工作节点区域，即使应用没有已部署服务也会展示。缺少区域信息时显示 `--`；这是只读信息，不作为应用配置提交。

## 开发验证

```bash
pytest -q
node --check webui/app.js
```

浏览器回归测试在本机 Playwright 和 Chromium 可用时运行，以拦截的 API 响应验证表单请求、确认弹窗、等待状态、同步成功/失败和环境变量保护；不会创建真实构建或推送镜像。缺少浏览器依赖时会明确跳过对应测试。
