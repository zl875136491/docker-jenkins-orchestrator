# code-server、Homepage、Dashboard 复测验收记录

## 1. 验收范围

- 验收日期：2026 年 10 月 27 日（Asia/Shanghai）。
- 目标环境：`.111`，服务入口为 `http://10.32.12.111:8088`。
- 测试对象：内部 GitLab 中保存的三个开源项目镜像：
  - `e2e-code-server-20261024`
  - `e2e-homepage-20261024`
  - `e2e-dashboard-20261024`
- 当前编排项目版本：`3fb429c`（工作区和 `origin/main` 在测试前均干净）。
- 本轮没有修改应用源码、应用 Compose、编排项目源码或 `.env`；只使用已有应用配置发起新的隔离检查和构建。
- 本文不记录 API Key、密码、访问令牌、GitLab/Jenkins/Harbor 凭据。

本轮使用已有应用 ID 是为了复测当前系统中真实保存的项目配置；每个项目都创建了新的 Build ID 和 Jenkins 构建编号，没有覆盖历史构建，也没有把历史结果当成本轮结果。

## 2. 为什么按这个流程测试

系统的交付门禁不是“能拉到镜像”就算通过，而是按以下顺序工作：

1. 读取应用中保存的 Git 仓库、分支、Compose 和 `require_route`。
2. 针对固定的完整 Git commit 执行路由静态检查，确认目标路由存在且代码没有未审阅修改。
3. 只有静态检查通过，才创建新的 Build 并交给 Jenkins。
4. Jenkins 构建并部署到 Swarm 后，由 Worker 使用当前 Build 的真实服务和端口执行动态 HTTP 探针。
5. 对具备交互登录行为的 code-server，动态探针之外增加真实浏览器登录和刷新验证。
6. 验收结束后按 `build_id` 清理服务、端口、网络、镜像和 workspace；不执行全局 Docker prune，不删除其他应用的服务。

这样做可以分别回答四个问题：应用源代码是否满足路由检查、构建链路是否可用、部署后的服务是否真的可访问、资源是否能安全回收。若只看 Jenkins 的 `SUCCESS`，会漏掉运行时 500、登录失败和资源残留。

## 3. 执行过程

### 3.1 环境预检

- 只读检查 `.111` 的 API、Worker、Beat、MongoDB、Redis，五个容器均为 `healthy`。
- 检查 Docker Swarm：状态为 `active`，当前节点为可用 Manager。
- 检查当前服务列表，确认已有应用仍在运行；没有执行停止已有服务、重置 Swarm 或全局清理。
- 通过控制台 API 读取三个应用的非敏感元数据和当前路由：
  - Homepage：`/api/config/custom.css`
  - code-server：`/healthz`
  - Dashboard：`/api/health`
- 环境预检的目的，是避免把“平台没启动”“Swarm 未启用”“路由未配置”误报为项目源码错误。

### 3.2 强制静态检查

对三个应用分别调用静态检查 API，并等待 `running` 到最终状态。结果如下：

| 项目 | 检查 ID | 固定 commit | 结果 |
| --- | --- | --- | --- |
| Homepage | `ca5d858995a3476a9583a81ace01b615` | `7f4f58c018e40ee8179c93cc20c7d85c4f3df5b8` | 通过 |
| code-server | `0698252e6a61414eaf98392d0da503d0` | `993063ea374d88c5a1f4f1b21efdc3345a1587d3` | 通过 |
| Dashboard | `d24fb665d4734900a5a7f16e402fe2f7` | `5f37fea2c4d1cabadf2cc93d722018492eaa65bf` | 通过 |

静态检查通过表示：在本轮固定的提交和路由上，系统检查没有发现需要先修改源码的阻塞问题。它不等于应用已经部署成功，也不等于所有运行时配置正确，所以仍继续构建和动态验证。

### 3.3 构建、部署和动态检查

#### Homepage

- Build ID：`06f49cf338d740edb4c41a9ecdeb917b`
- Jenkins：`#107`
- 静态检查：通过。
- 构建和部署阶段：服务曾成功创建并进入动态检查。
- 动态路由：`GET /api/config/custom.css`
- 动态结果：失败，HTTP `500`。
- 清理结果：完成，服务状态为 `cleaned`，所有本轮资源状态为 `removed`。

系统保存的失败证据显示：

- 请求本身就是配置的目标路由，没有被错误地改写。
- 服务返回的是 Next.js `500 Internal Server Error`，不是连接超时、DNS 失败或端口不可达。
- 容器日志反复记录无法创建 `/app/config`，随后读取 `/app/config/settings.yaml` 失败，错误为 `ENOENT`。

因此 Homepage 本轮结论是：**静态检查通过、Jenkins 构建链路可运行，但应用运行时配置目录/文件缺失，动态验收失败。**

#### code-server

- Build ID：`1a89c317546b494b8cb49a7bdf32cd87`
- Jenkins：`#108`
- 静态检查：通过。
- 动态路由：`GET /healthz`
- 动态结果：通过。
- 构建入口：`http://10.32.12.111:19004`
- 入口直接探测：HTTP `302`，进入登录流程，说明服务已监听且登录保护生效。
- 浏览器验证：
  - 登录页出现密码输入框；
  - 使用应用配置中的密码登录成功；
  - 登录后出现 code-server 工作台；
  - 刷新页面后仍停留在工作台，没有回到登录页；
  - 刷新后的路径保持在 code-server 工作区路径。
- 清理结果：完成，服务状态为 `cleaned`，所有本轮资源状态为 `removed`。

这一步专门验证了仅 HTTP 健康检查无法覆盖的登录态和刷新行为，也验证了应用配置中的环境变量确实进入了本次部署的 Swarm Service；密码值未被读取到文档或日志中。

#### Dashboard

- Build ID：`c5c2d901a4004e7e9a8ee39d6113fe59`
- Jenkins：`#109`
- 静态检查：通过。
- 动态路由：`GET /api/health`
- 动态结果：通过。
- 入口：`http://10.32.12.111:19002`
- 入口直接探测：HTTP `200`，响应体约 `73,869` 字节。
- 健康路由直接探测：HTTP `200`，响应体 `{"status":"ok"}`，大小 `15` 字节。
- 清理结果：第一次清理在 workspace 资源上出现暂态 `partial_failed`；确认服务、网络、镜像和端口均已删除后，重试同一个 Build 的清理 API，最终状态变为 `succeeded`，workspace 快照也已消失，全部资源状态为 `removed`。

Dashboard 的根页面可以正常返回，但它不是本系统动态探针的合适健康路由：编排项目默认限制单次 HTTP 响应为 `65,536` 字节，根页面超过该限制；轻量 `/api/health` 返回 15 字节并通过探针。因此本轮不能把“大页面响应超过探针上限”误报成 Dashboard 服务不可用。

## 4. 结果汇总

| 项目 | 静态检查 | Jenkins | 动态检查 | 浏览器 | 本轮入口 | 清理 |
| --- | --- | --- | --- | --- | --- | --- |
| Homepage | 通过 | 构建/部署进入动态阶段 | 失败：目标路由 HTTP 500 | 未执行，服务已因动态失败回收 | 无可用入口 | 成功 |
| code-server | 通过 | `#108` 成功 | `/healthz` 通过 | 登录、刷新均通过 | `http://10.32.12.111:19004` | 成功 |
| Dashboard | 通过 | `#109` 成功 | `/api/health` 通过 | 未要求登录；入口和健康 API 均通过 | `http://10.32.12.111:19002` | 成功 |

本轮的 Homepage Jenkins 编号为 `#107`，最终 Build 状态为 `failed`，失败发生在动态应用检查而不是静态检查或 Jenkins 构建准备阶段。

## 5. 错误归因：反馈指引、编排项目还是应用项目

### 5.1 属于本项目编排逻辑或平台能力的部分

- **静态检查先于构建**：这是本项目的交付门禁设计。三项目本轮均通过，说明当前静态检查调度、固定 commit 和结果持久化链路正常。
- **动态探针响应大小限制**：本项目默认 `ORCHESTRATOR_ROUTE_PROBE_MAX_RESPONSE_BYTES=65536`。这是探针保护机制，不是 Dashboard 源码错误；应用应提供轻量健康路由，不能用完整首页替代健康接口。
- **应用环境变量注入**：code-server 本轮实际登录成功，说明当前 Worker 将应用配置中的声明变量解析并传入部署服务的链路有效。本轮没有再次修改这部分代码，只是通过真实服务验证当前版本行为。
- **资源清理**：Dashboard 首次清理出现 workspace 资源暂态失败，重试同一清理 API 后成功。这属于本项目资源清理/执行时序的运行表现，不是 Dashboard 应用错误；最终没有残留本轮服务、容器、端口、网络或 workspace 快照。该过程应保留在验收记录中，不能只记录最终成功而隐藏第一次异常。

### 5.2 属于应用项目或应用运行配置的部分

- **Homepage 的 500**：动态请求已经命中正确路由，服务进程也能响应，但应用运行时找不到 `/app/config/settings.yaml`。从该项目当前 Dockerfile 和 Compose 内容看，没有看到把配置目录/`settings.yaml` 放入镜像、通过挂载提供，或在启动时创建可用配置的定义。应用日志中的 `ENOENT` 与该源码/镜像配置事实一致。
- 所以 Homepage 需要由应用项目维护者或部署配置维护者补充配置文件、配置目录或有效挂载，再用原 commit/更新后的 commit 重跑动态检查；不能通过修改探针请求参数解决。
- **Dashboard 的首页体积**：Dashboard 根页面返回 200，并非应用不可用；使用专用 `/api/health` 是应用与探针约束之间的正确接口选择。

### 5.3 属于“用户侧可修复”还是“系统上层依赖”

- Homepage 本轮的失败不是用户输入的路由、参数或凭据错误，也不是 Jenkins 排队或 Docker 网络错误；它是应用运行包缺少自身依赖配置文件的上层应用配置问题。
- code-server 和 Dashboard 没有发现需要用户修复的阻塞。
- 本轮没有把失败指引简单归类为“用户代码有问题”：Homepage 的静态检查已经通过，真正的失败证据来自部署后应用日志和 HTTP 响应。

## 6. 清理与环境影响

- 本轮创建的三个 Build 均已完成清理：
  - `06f49cf338d740edb4c41a9ecdeb917b`
  - `1a89c317546b494b8cb49a7bdf32cd87`
  - `c5c2d901a4004e7e9a8ee39d6113fe59`
- 三个 Build 的最终 `cleanup_status` 均为 `succeeded`，服务记录均为 `cleaned`，资源状态均为 `removed`。
- Harbor 镜像标签逻辑引用已解除，记录显示 `registry_storage_status=waiting_gc`；这表示等待 Harbor 既有垃圾回收机制处理物理 blob，不表示本轮仍有可访问服务或编排残留。
- 本轮专属 Swarm 服务、容器、端口、网络和 workspace 快照均已核对不存在。
- `.111` 的 API、Worker、Beat、MongoDB、Redis 在收尾时仍为 `healthy`；没有重启或修改这些基础服务。

## 7. 最终结论和后续建议

1. **code-server：通过。** 静态检查、构建、动态健康检查、真实登录和刷新保持均通过。
2. **Dashboard：通过。** 静态检查、构建、`/api/health` 动态检查和外部入口探测均通过；根页面较大是探针限制场景，不是服务不可用。
3. **Homepage：本轮不通过。** 静态检查通过，构建链路可运行，但 `/api/config/custom.css` 返回 500；应用容器缺少 `/app/config/settings.yaml`。应补齐应用配置目录/文件或正确挂载后再复测。
4. 本轮没有新增或修复代码；文档结论来自 `.111` 的实际 API、Jenkins/Build 状态、Swarm 服务状态、动态响应、应用日志和源码/Compose 结构交叉验证。
