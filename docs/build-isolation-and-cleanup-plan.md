# 构建资源隔离与清理统一开发计划

日期：2026-10-14。状态：本地开发与回归已完成；未部署测试环境。第 1～10 节保留设计约定，第 11 节记录实现与验证范围。

## 1. 目标与边界

- 以 `build_id` 为部署、查询和清理的基本单位；沿用现有 APP、Build，不新增 Release、蓝绿切流或网关体系。
- 同一 APP 的多个构建可以同时运行；创建、重试、失败回滚、停止和清理构建 B，不改变构建 A 的资源或数据。
- 共享仓库来源、APP 元信息、平台控制服务；运行服务、容器、网络、端口、私有挂载、数据卷和构建制品引用按构建隔离。
- Git 检出目录、workspace 快照和运行时可写目录彼此分离。APP 上传暂存区只作为构建输入源，不供运行容器直接可写挂载。
- 清理接口只要求 `build_id`，不要求调用方提供 APP ID、节点、镜像名、服务名、目录或资源类型。
- 清理会停止该构建的服务，并删除其私有运行数据；这是销毁操作，不是仅停止服务。
- 保留 APP、仓库、APP 暂存上传文件、Build 历史、必要审计记录与清理结果。
- 初始阶段只生成计划；后续按指令实施本地代码。真实资源删除和测试环境部署仍需另行批准。

## 2. 改造前核查基线

| 实现点 | 现状 | 改造方向 |
|---|---|---|
| `orchestrator/docker_services.py` | 部署入口主要接收 APP；相同名称更新已有 service，网络和默认卷按 APP 命名 | 全链路增加构建作用域；只创建或恢复本构建资源 |
| `orchestrator/docker_services.py` | 成功部署后按 APP 查找旧 service 并清理；失败回滚主要删除本次新建 service/network | 取消跨构建旧服务清理；失败补偿只处理本构建资源 |
| `orchestrator/worker_runtime.py` | 完整部署成功后保存服务记录 | 资源创建前登记意图，创建后立即保存 ID，支持部分失败回收 |
| `orchestrator/workspaces.py` | 已有 `<app-namespace>/snapshots/<build_id>/files` 和同级 `workspace.tar` | 保留快照机制，补归属登记、只读挂载和精确删除 |
| `orchestrator/repository.py` | 镜像、服务记录含 `build_id`，但缺少完整运行资源清单及清理进度 | 新增构建资源清单、清理状态和端口预留记录 |
| 镜像交付 | Jenkins 使用构建号生成 tag；现有适配器没有覆盖本计划所需的完整删除流程 | 传递平台 `BUILD_ID`，生成构建专属引用，增加精确本地/Harbor 清理 |
| 测试 Swarm | 核查时为单节点 | 首期覆盖现有拓扑；记录执行端，禁止把单个 Docker daemon 的清理结果当作多节点全量结果 |

本轮未提供知识图谱 MCP 工具，核查采用定向源码读取与只读运行环境检查，不声称完成全仓库审计。

## 3. 对外接口：仅 build_id

保持当前接口命名风格，新增同一路径的两个方法。

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/v1/jenkins/build_resources/{build_id}` | 查看资源清单、独立服务入口、可清理范围、清理状态及错误 |
| `DELETE` | `/api/v1/jenkins/build_resources/{build_id}` | 异步销毁该构建的全部可确证私有资源 |

请求不需要 body 或 query 参数；沿用 Bearer 认证。节点、项目、镜像引用、端口和目录均由后端根据 Build 和资源清单解析。

### 3.1 默认行为

- 构建不存在：`404`；沿用现有输入校验和认证规则。
- 构建流程仍在排队、构建、部署，或外部任务仍会创建/推送资源：`409`，返回具体原因。本期不增加 `force` 或自动取消 Jenkins 的能力。
- 构建流程已结束而其服务仍在运行：允许清理，先停止并移除该构建服务；不需要另传停止参数。
- 首次受理：`202`，返回 `build_id` 和 `cleanup_status=queued`；Celery 执行耗时操作。
- 已排队或执行中：重复 `DELETE` 返回当前任务状态，不重复启动同一清理。
- 已全部逻辑清理：重复 `DELETE` 返回 `200` 与已有结果，不再次执行破坏操作。
- 部分失败：再次调用同一个 `DELETE` 即可重试，只补做剩余项。
- 无可用队列或受理记录保存失败：明确返回失败，不能宣称已经受理。
- 使用同一个 `GET` 轮询，不要求调用方再提供 operation ID。

首次受理示例：

```json
{
  "build_id": "<build_id>",
  "cleanup_status": "queued",
  "message": "清理将停止该构建服务并删除其私有运行数据"
}
```

### 3.2 查询结果

`GET` 返回以下信息，具体字段在实现时形成 Pydantic 模型与 OpenAPI 契约：

- `build_id`、`appid`、构建结果与 `cleanup_status`。
- 按类型列出的资源、独立入口、已删除项、剩余项、受保护项。
- 各资源的结果和可重试错误；不输出凭据或删除命令。
- `started_at`、`finished_at` 等业务时间继续使用东八区 `+08:00`。
- 存储统计与统计口径；无法确认实际释放字节数时返回 `null`，不把镜像虚拟大小相加当作磁盘释放量。
- Harbor 存储状态单独表达，例如 `waiting_gc`、`shared_retained`、`unknown`，不与本地资源删除混为一谈。

`cleanup_status` 建议使用 `not_started`、`queued`、`running`、`succeeded`、`partial_failed`。历史 Build 的 `succeeded/failed/cancelled` 不因清理被改写。

## 4. 构建隔离与资源登记

### 4.1 统一资源身份

- 物理资源名由完整的 `(appid, build_id, logical_name)` 生成，超长名称沿用受限长度与稳定哈希机制；不能仅截取 build ID 前几位。
- service、task container、network、volume 都记录系统管理标识、原始 `appid`、完整 `build_id`、逻辑资源名；保留字段不能由用户 Compose 覆盖。
- service 标签与 task container 标签分别设置，不能假定 service 标签会自动成为容器标签。
- 镜像通过构建专属引用、digest 和资源清单登记归属；不能把一个可被多个 tag 引用的 Docker image ID 直接判定为某构建独占。
- 同一版本重试只恢复自己的资源；不得更新其他构建的 service 或重新绑定其挂载。

### 4.2 不可变构建输入

- 固定 Compose、仓库 URL、Git 引用和已解析 SHA、加密配置快照或密钥版本引用、workspace 校验和。
- 第一次固定 SHA 后，重试不再重新解析移动分支；APP 后续修改不会改变已创建构建的配置。
- Jenkins 接收平台 `BUILD_ID`；构建号保留为追踪信息，不再作为唯一制品归属依据。
- 构建制品使用包含完整 build 身份的唯一 tag，并记录 digest；禁止复用可覆盖的公共发布 tag。
- 每个构建使用自己的可写检出/构建目录，pipeline 收尾只清理自己的目录。

### 4.3 网络、挂载和端口

- 每个构建创建私有 overlay network；Compose 内部别名只在本构建网络中使用。
- named volume 显式创建和登记，避免依赖 Docker 自动创建没有构建归属的卷。
- workspace 输入只读挂载；可写数据使用构建专属目录或卷，不写入 APP 暂存区或输入快照。
- `external` 卷/网络、显式物理卷名、可写绝对路径和自定义卷驱动配置需要预检：无法保证独立后端数据路径就报错，不能仅改卷名后声称已隔离。
- 指向同一外部数据库或存储的配置不因构建名不同而自动隔离。无法验证或提供构建独立目标时，明确阻塞，不自动克隆外部业务数据。
- 在 MongoDB 预留外部端口，使用唯一约束避免平台内并发分配同一执行目标和协议的端口；同时检查 Docker 实际占用并处理外部竞争。
- 容器内部端口可以相同；对外返回实际分配的端口。确认 service 删除和实际绑定释放后再释放端口预留。
- 构建级物理映射必须贯穿模板、Jenkins artifact、拓扑验证和 Worker，不能只修改某一侧造成契约不一致；不改写用户仓库或 APP 的源 Compose。

### 4.4 持久化清单

新增以 `build_id` 唯一索引的构建资源清单，记录：

- 资源类型、逻辑名、物理名、不可变原生 ID 或引用、执行目标/节点、归属证据。
- 创建前的计划项与创建后的实际结果，包括失败构建留下的卷、网络、镜像引用和目录。
- 资源状态、清理进度、错误、租约、清理代次、开始/结束时间。
- 创建标签明确但回写失败的资源，通过完整 build 标签或已登记的精确路径补扫；禁止按 APP 或名称前缀宽泛删除。

在资源创建前保存意图，创建后立即记录 ID，不等待整套部署成功。现有服务和镜像历史记录继续保留，增加已回收状态或从资源清单关联状态。

## 5. 清理流程与安全规则

### 5.1 执行顺序

1. 校验构建流程与外部执行已静止，持久化清理请求并取得构建级执行权，禁止后续任务为该 build 新建资源。
2. 读取清单，补充核对完整 build 标签、任务节点与实际使用关系；先收集任务/container ID，不能依赖 service 删除后仍可查询其全部任务。
3. 删除本构建 service，等待任务停止；收尾处理经 service ID、container 标签和清单共同验证的残留容器及其可写层。
4. 确认无本构建或其他构建容器使用后，删除构建私有卷、运行目录、workspace 快照及 archive。
5. 确认没有附着使用者后删除构建私有 network；验证端口不再绑定后释放预留。
6. 按执行端清理本构建本地镜像引用；删除 Harbor 中本构建专属 tag，并检查同 digest 的其他引用。
7. 再核对清单与实际资源。更新逐项结果、历史服务可用性和清理状态；遗留项返回错误，不能把部分完成标记成全量成功。

无关资源分支可以继续清理；有依赖的资源必须等待前置资源释放。例如容器未停止时，不强制删其卷或镜像。

### 5.2 安全约束

- 删除前重新按原生 ID 获取对象并校验归属，防止同名资源已被替换。
- 禁止 `docker system prune`、全局 `image/volume/network prune`、共享 builder cache prune，以及按 APP 删除全部资源。
- 不强制删除仍被引用的卷、镜像或网络；不确定归属的资源保留并报告原因。
- 文件删除限定在登记的构建私有根目录，验证路径边界，不跟随符号链接越界；不删除 APP 暂存区、仓库来源或其他构建目录。
- 对单项 `404/not found` 按幂等已删除处理；权限不足、超时和执行端不可达属于真实失败。
- 部署重试和清理通过同一构建级状态/租约协调；端口分配还需要跨构建唯一约束。清理开始后旧任务不能恢复部署。
- Worker 重启后从 MongoDB 恢复剩余步骤；清理成功后保留阻止旧任务重建资源的状态。
- UI 确认不能替代服务端归属验证。API 不接受任意 Docker ID 或任意路径作为删除目标。

### 5.3 历史构建

已有资源主要是 APP 级归属，不能因为历史服务记录含某个 `build_id` 就视为该构建独占。

- 新构建采用新的构建作用域，不对现有服务做原地改名或强制迁移。
- 可确证归属的历史 workspace 快照和制品引用可以登记后清理。
- 多个构建共用的 service ID、APP 级网络/卷、归属不明镜像不自动删除，列为受保护或阻塞项。
- 不完整清理返回 `partial_failed` 和具体资源，不增加绕过归属检查的 `force` 参数。

## 6. 镜像与物理存储回收口径

### 6.1 本地 Docker

- 清理部署节点和已登记构建执行端上的构建专属引用，检查所有容器和其他构建引用，不做强制镜像删除。
- 删除本构建 tag 不等于所有共享层都释放。公共只读基础镜像与仍被其他构建使用的层保留。
- service 删除不是卷回收的替代步骤；残留容器、卷、目录和本地镜像必须分别核对。
- 不宣称能够按 build 精确回收所有共享 Docker/BuildKit 缓存；只有可确证私有归属的缓存目录属于本接口清理范围。

### 6.2 Harbor

- 默认删除本构建专属 tag，不能把按 reference 删除整个 artifact 的接口当作仅删除一个 tag。
- 同 digest 还有其他 tag、构建引用或制品关联时，保留 artifact；不得误删其他构建制品。
- 无其他引用的构建制品按校验后的策略解除引用或标记待回收；物理 blob 释放依赖 Harbor GC。
- 不为每次 build 清理触发一次全项目或全局 GC。实施前核验 Harbor 版本、删除权限、tag 保护策略和现有定期 GC；未配置时明确显示等待 GC。
- 逻辑清理完成与物理存储回收分别报告，不能把 tag 删除响应当作磁盘空间已释放。

### 6.3 执行端范围

Docker service/network 是集群级资源；本地容器、卷、目录、镜像缓存需要在实际节点核验。后端记录执行目标，不要求用户传节点参数。

首期覆盖现有单节点测试 Swarm，并核验 Jenkins 构建执行端的回收通道。Jenkins 私有目录由 pipeline 收尾清理；需后续回收的构建专属镜像/目录应登记执行端并通过受控执行通道处理。不可达目标返回剩余资源与错误，不静默跳过。未来多节点接入沿用同一对外 API，不在本期增加完整节点代理平台。

## 7. 开发任务与依赖顺序

各阶段均是待开发任务，不是本轮已完成项。

| 阶段 | 开发内容 | 主要涉及文件 | 完成标准 |
|---|---|---|---|
| P1：归属与状态基础 | 构建资源清单、资源状态、清理租约/代次、端口预留、不可变输入 | `orchestrator/models.py`、`orchestrator/repository.py`、`orchestrator/naming.py`、`orchestrator/services.py`、相关仓储/命名测试 | 能登记计划与部分创建结果；并发端口分配无重复；输入快照不会被 APP 修改影响 |
| P2：构建独立部署 | service/task 标签，私有 network/volume/mount，Jenkins BUILD_ID、独立制品引用、按构建补偿，取消 APP 级旧服务删除 | `orchestrator/docker_services.py`、`orchestrator/workspaces.py`、`orchestrator/worker_runtime.py`、`orchestrator/jenkins.py`、`jenkins/orchestrator-build.groovy`、模板映射及相关测试 | 同 APP 两构建同时运行，名称/挂载/网络/端口独立；新版失败或重试不影响旧版 |
| P3：清理执行器 | 新增集中式构建清理服务，精确 Docker/Harbor 删除，目录安全删除，恢复与逐项结果 | 新增 `orchestrator/build_resources.py`、`orchestrator/harbor_cleanup.py`；调整 Docker/workspace 适配器、Worker、`orchestrator/worker_tasks.py` 和恢复调度 | service、容器、私有卷/目录、network、端口、镜像引用按顺序回收；失败可重试，不误删其他构建 |
| P4：接口与前端 | GET/DELETE 接口、构建级服务入口、资源详情与清理按钮，清理状态展示 | `main.py`、`orchestrator/container.py`、`webui/app.js`、`webui/index.html`、必要样式及 API/浏览器测试 | 清理只传 build_id；202 后可轮询；UI 明确提示停止服务和删除数据；已清理入口不再标记可用 |
| P5：回归与交付准备 | 端到端隔离/清理测试、历史资源保护、故障恢复、API 文档和环境配置核验 | 现有 Docker/Worker/Jenkins/workspace 测试；新增构建清理测试；`docs/api-reference.md`、`docs/api-call-flow.md`、`docs/workspace.md` | 全部验收场景通过；输出变更与风险报告，等待上线批准 |

依赖关系：P1 → P2 → P3 → P4 → P5。隔离和资源登记必须先于清理开放，不能先提供一个按 APP 扫描删除的临时接口。

## 8. 必须覆盖的测试与验收

以下清单同时作为待批准的真实环境验收门禁，不能用模拟 Docker/Harbor 断言代替真实容器和 HTTP 验证。对应场景的本地单元、API 和浏览器测试已实现，证据见第 11 节；本轮不勾选真实环境验收。

- [ ] 同一 APP 的构建 A/B 同时访问正常；service/container/network/私有卷/目录独立，镜像引用不覆盖。
- [ ] A/B 并发创建时外部端口无冲突；实际端口与 API 返回一致。
- [ ] B 部署失败、重复任务或重启恢复时，A 的服务 ID、镜像引用、数据、网络和端口不变。
- [ ] 清理 B 后，其 service 和容器不存在，私有卷/目录/network 消失，端口可重新分配；A 仍正常。
- [ ] 失败构建在部分资源创建后可通过清理接口回收，不依赖部署成功记录。
- [ ] 相同清理重复调用幂等；外部资源已删除不报破坏性错误。
- [ ] 构建或外部任务仍在创建资源时 DELETE 返回 409；终态构建的运行服务可正常销毁。
- [ ] 清理进行中收到旧部署消息，不能重新创建资源；清理后旧任务也不能复活构建。
- [ ] 容器占用、权限不足、Harbor 不可达、节点不可达等返回部分失败；重试只处理剩余项。
- [ ] 同 digest 多 tag、多个构建引用同一底层 image 时，只移除 B 的引用，A 可继续运行和重新拉取。
- [ ] service 移除后显式处理卷；不能只删服务就报告所有存储已回收。
- [ ] APP 暂存文件、仓库来源、APP 配置、基础镜像、历史 Build 和其他构建不被清理。
- [ ] 归属标签被改动、同名对象被替换、路径越界/符号链接攻击时拒绝删除。
- [ ] 历史 APP 级共享资源受到保护；不能按关联 Build 记录推断独占。
- [ ] 所有新增时间字段保持东八区输出，错误与日志不泄漏密钥。
- [ ] 不调用任何全局 prune；Harbor tag 删除与物理 GC 状态分开报告。

验证顺序：定向单元测试 → API/前端浏览器测试 → 仓库回归测试。真实环境验证只能在用户批准后创建专用验证 APP/Build，不使用现有业务构建做删除实验。

## 9. 上线约束

- 本地已实现隔离、资源清单、异步清理和控制台；未删除真实资源或更新测试环境。
- 开发完成后先提交验证结果供用户审阅，不自动上线测试环境。
- 获得明确批准后，再同步升级 API、Worker、Beat 与 Jenkins 的 BUILD_ID/制品契约，避免新旧执行逻辑混用。
- 上线前对历史资源做只读归属盘点；不自动重建、改名或清理现有业务资源。

## 10. 官方依据

实施时按环境实际版本核对以下接口和生命周期约束：

- Docker SDK service 与 task container 标签分别配置：`https://docker-py.readthedocs.io/en/stable/services.html`。
- Docker volume 生命周期独立于容器：`https://docs.docker.com/engine/storage/volumes/`。
- 本地镜像删除、untag 和共享层行为：`https://docs.docker.com/reference/cli/docker/image/rm/`。
- 全局 prune 的清理范围：`https://docs.docker.com/reference/cli/docker/system/prune/`。
- Harbor tag 与 artifact 删除 API：`https://github.com/goharbor/harbor/blob/main/api/v2.0/swagger.yaml`。
- Harbor 逻辑删除与物理 GC：`https://goharbor.io/docs/main/administration/garbage-collection/`。

## 11. 本地实现与交付说明

### 11.1 实现对应关系

| 阶段 | 已落地能力 | 本地验证 |
|---|---|---|
| P1 | 完整 APP/Build 派生 namespace；不可变配置和 workspace 快照；准备完成标识；资源意图；Mongo 唯一索引、端口预留、CAS 与续租 | `tests/test_build_resources.py`、`tests/test_resource_adapters.py`、`tests/test_worker_runtime.py` |
| P2 | 构建专属 service/container 标签、network、卷、镜像 tag、节点约束、端口；禁止跨构建 stale cleanup；输入强制只读；拦截共享后端 | `tests/test_build_resources.py`、`tests/test_complex_project_delivery.py`、Docker/Worker/workspace 回归 |
| P2 制品 | Jenkins 显式接收 BUILD_ID 并映射 PLATFORM_BUILD_ID；构建独立目录；校验镜像 digest、checkout SHA 与 artifact 拓扑；post 资源报告和精确收尾 | `tests/test_jenkins.py`、`tests/test_jenkins_pipeline.py` |
| P3 | 按依赖清理；Jenkins 静止复核；Docker 执行端身份校验；旧资源保护；失败重试；过期租约恢复；保留历史；Harbor 只删 tag | `tests/test_build_resources.py`、`tests/test_resource_adapters.py` |
| P4 | 同路径 GET/DELETE；认证、404/409/503、202/200；构建入口；控制台不可逆确认、轮询、切换取消和退出清空 | API 测试与 `tests/test_webui_browser.py` |
| P5 | API/流程/workspace 文档、system-guide、环境变量与 Compose 接线；本地完整回归 | `tests/test_deployment_config.py`、全量 pytest、Node 语法与 Python 编译检查 |

新增资源模型、端口预留和租约同时实现内存与 Mongo 两种存储。Mongo 测试使用 mongomock 执行实际仓储方法，不替代真实 Mongo 拓扑的上线验收；开发依赖使用 `requirements-dev.txt`。浏览器测试运行实际 HTML/JS，API 由测试桩提供。

安全回归覆盖：同 APP 的 A/B 并存；B 失败/清理不改变 A；端口并发与崩溃补登记；重复清理；队列故障；外部执行未结束；过期/丢失租约；同名资源被替换；卷创建身份变化；目录符号链接；其他容器仍挂载目录；共享镜像内容；未知执行端；历史 APP 资源保护；部分失败只补做剩余项。

复杂项目测试不再把宿主绝对路径视为可隔离资源：在测试副本中显式改为构建私有卷，再执行交付测试；保留原始项目样本，不能据此声称原项目外部共享存储已被自动迁移。

### 11.2 运行边界与异常处理

- 本期以可识别的单节点 Swarm 存储位置部署新构建。独立 builder/其他节点必须通过 `ORCHESTRATOR_CLEANUP_DOCKER_TARGETS` 提供受控执行端，目标身份不匹配或不可达时保留资源并报告部分失败。
- 端口预留基于执行端、协议和端口的唯一索引，并检查 Swarm 与容器的绑定。Docker 外部并发占用仍可能在检查后发生，服务创建失败时保留登记供精确回收，不强制抢占其他工作负载。
- Jenkins 触发请求结果未知且未保存 queue URL 时，明确阻塞清理，不能把网络超时推断成未创建外部任务。需先由运维核验外部执行；不自动重复触发。
- Jenkins post 报告必须证明构建归属、镜像清单和私有目录收尾。报告缺失、不完整、过期或目录删除未验证时保留错误，不把未知 builder 存储视为已释放。
- 外部状态型连接预检覆盖常见数据库、缓存、消息与对象存储变量/DSN；它不是对任意源码、启动命令或配置文件的完整数据流审计。自定义连接配置仍须评审，不能据此承诺业务层外部数据自动隔离。
- 没有创建共享可写运行目录的旁路：可写数据仅使用构建私有本地卷；workspace 为只读输入。无可靠归属的任意宿主目录不接受 API 清理。
- 私有卷以 name、完整 owner 标签和 Docker 创建时间共同校验。未知引用、其他服务/容器使用和归属变化都拒绝强删。
- `reclaimed_bytes` 返回 null；Harbor 状态单独记录。没有触发全局 prune、共享 cache prune 或 Harbor 全局 GC。

### 11.3 获批后的上线门禁

1. 暂停新任务入口并只读盘点旧资源；同步发布 API、Worker、Beat 与 Jenkins pipeline，避免旧 APP 级 worker 消费新构建。
2. 核对 Mongo 新索引、API 的 Jenkins 查询权限、Worker 的 Docker/Harbor 精确删除权限，以及实际 daemon/node 身份映射。
3. 对完整 Jenkinsfile 执行环境内 Declarative lint 和受控 Job 配置回读；本地嵌入脚本测试不等于此门禁。
4. 在专用验证 APP 下创建 A/B，核验真实 HTTP、服务/容器/卷/网络/端口和输入数据；只清理 B，并复核 A 的 ID、数据和访问正常。
5. 验证 builder 残留、本地镜像 tag、Harbor tag/共享 digest 与 GC 策略；记录可确证回收和仍被保留的存储，不使用现有业务 Build 做删除实验。

未获批准前不执行这些环境操作。历史的 APP 级部署不会被自动改名、迁移、重建或销毁；需要隔离的版本应创建新 Build。

### 11.4 最终本地验证结果（2026-10-14）

- `pytest -q --tb=short`：736 passed；1 个既有 Docker 依赖的 distutils 弃用警告。实际浏览器测试和 mongomock 仓储测试均执行。
- `python3 -m compileall -q main.py orchestrator tests`、`node --check webui/app.js`、`git diff --check`：通过。
- `docker-compose.yml` 与 `docker-compose.test.yml` 的 `docker compose config --quiet`：使用占位配置和 `--env-file /dev/null` 验证通过，不连接部署端点，不读取测试环境凭据，不启动服务。
- 未部署、重启测试环境，未触发真实 Jenkins 作业，未执行真实 Docker/Harbor 删除；完整 Jenkins Declarative lint、Job 同步与真实 A/B 资源验收属于第 11.3 节的获批后门禁。
