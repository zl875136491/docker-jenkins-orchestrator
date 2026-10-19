# APP workspace

语言模板现在统一生成本地 `build.context` + `build.dockerfile`；APP 注册时预留独立 workspace 目录。上传文件既可以被 Dockerfile 复制进镜像，也可以按需只读挂载到运行容器。上传、构建和运行使用同一套 workspace 契约，不需要 Apps 知道宿主路径。

## Apps 调用协议

1. `POST /api/v1/jenkins/app_create` 注册 APP，保存 Compose。仅上传源码应用可省略 `repository_url`；历史 image-only APP 仍可读取，但新构建必须补齐本地 `build` 和 Dockerfile。
2. `POST /api/v1/workspace/<appid>` 上传文件；`POST /workspace/<appid>` 为同义入口。均需原有 Bearer access token，未注册返回 404。
3. `GET /api/v1/workspace/<appid>` 查看暂存文件的 path、size、sha256 和 total_bytes。
   `DELETE /api/v1/workspace/<appid>?path=<relative-file-path>` 删除指定暂存文件，成功返回 HTTP 204。需要同一 Bearer token；只支持单个文件，不递归删除目录，不修改已经冻结的构建快照。文件或 APP 不存在返回 404，非法路径、目录或符号链接返回 422，存储不可用返回 503。
4. 全部文件准备后调用 `build_create`。服务端冻结 Compose 与文件快照，返回的 `workspace_snapshot` 等于 build_id。
5. 按原协议轮询构建。后续上传只修改暂存区，不影响已经创建的构建或运行实例；要发布更新须新建构建。

上传使用 `multipart/form-data`，一个或多个同名 `files` 字段（兼容单文件字段 `file`）。filename 为相对 POSIX 路径，可带目录；可选文本字段 `path` 给所有文件添加相对目录前缀。覆盖同路径文件，不删除其他文件。不自动解压上传的 zip/tar，避免隐含路径与权限语义。

```bash
curl -X POST "$API/workspace/$APPID" \
  -H "Authorization: Bearer $TOKEN" \
  -F 'files=@./main.py;filename=main.py' \
  -F 'files=@./settings.json;filename=config/settings.json'
```

成功 HTTP 201 示例：

```json
{"appid":"demo","files":[{"path":"main.py","size":15,"sha256":"..."}],"total_bytes":15}
```

错误：401 未认证；404 APP 不存在；413 上传/存储额度超限；415 请求不是 multipart；422 非法路径/文件冲突；503 存储不可用。默认单请求文件内容合计 100 MiB、单 APP 暂存文件 1 GiB、10000 文件。对应配置 `ORCHESTRATOR_WORKSPACE_MAX_UPLOAD_BYTES`、`ORCHESTRATOR_WORKSPACE_MAX_BYTES`、`ORCHESTRATOR_WORKSPACE_MAX_FILES`。HTTP multipart 包装额外允许 1 MiB。路径禁止绝对路径、`..`、`~`、反斜杠、控制字符、冒号、`.git` 和符号链接，不能用上传修改其他 APP 的文件。继承平台 conductor 认证模型，不新增终端用户级 ACL。

## 控制台文件管理

1. 打开 `/ui/` 并连接控制端，在「当前应用」中选择已注册 APP，例如 `APP-20261012-40926F61`。
2. 进入「文件管理」分页，自动读取该 APP 的暂存文件；支持手动刷新、路径筛选，以及每页 20 / 50 / 100 个文件的列表分页。列表分页在浏览器内进行，列表 API 仍返回完整清单。
3. 多选文件或选择整个目录，按需填写目标目录前缀。目录上传以所选目录为根，保留内部子目录，避免额外套一层本地目录名；单文件可通过前缀上传到 `config` 等目录。同路径文件覆盖，目录上传不支持的浏览器仍可多选文件分批上传。
4. 点击「上传文件」，成功后自动刷新列表。列表展示相对路径、大小和 SHA256（悬停可看完整哈希）。点击行内「删除」并确认后，仅删除该 APP 的指定暂存文件，随后刷新清单。
5. 确认源码、配置和 Compose 所需挂载路径均存在，再进入「构建」创建新任务。`Workspace mount source is empty: .` 表示创建该次构建时尚未上传所需文件；之后补传不会修改失败构建的快照，也不会自动清除历史告警，必须重新创建构建。

切换应用或退出登录会清空页面上的文件列表和上传选择，并取消对旧请求结果的显示；已发送的上传或删除仍可能在服务端完成，返回该 APP 后应刷新清单确认。

## Compose

```yaml
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    image: example/app:local-build
    working_dir: /workspace
    command: [python, main.py]
    volumes:
      - .:/workspace:ro
      - ./config/settings.json:/etc/app/settings.json:ro
```

`.` 是该 APP 本次构建的上传文件根目录，不是仓库或 Jenkins 当前目录。源码服务必须在 Compose 中显式声明 `build.context` 和 `build.dockerfile`；Dockerfile 必须上传到对应 context 内，并负责复制源码及安装依赖。支持目录/单文件以及长语法 `type: bind` 的只读挂载。不存在或空的源目录在 Worker 验证阶段明确失败，不会创建空目录掩盖漏传文件。普通 named volume 保持原行为，数据库等运行数据仍应使用 named volume。

只靠上传文件构建镜像：

```yaml
x-orchestrator-build-source: workspace
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    ports: ["8080:8080"]
    volumes: ["./config:/etc/app:ro"]
```

上传 Dockerfile、源码、配置后构建，Jenkins 跳过 Git checkout，下载该 build 专属 archive，验证 SHA-256 后安全解包，再执行 Docker build。默认 `git` 模式先 checkout，再把上传内容覆盖到独立构建目录；Dockerfile 路径按 build context 解析。每个服务都必须有自己的本地 build 和 Dockerfile，image-only 不再接受。安装依赖应由 Dockerfile 或明确的构建步骤负责；仅挂源码不等于安装依赖。

## 存储与部署

宿主默认 `/srv/orchestrator/workspaces/<app-namespace>/files`，测试环境默认 `/srv/orchestrator-test/workspaces`。构建快照位于 `<app-namespace>/snapshots/<build_id>/files`，archive 与 files 并列。命名使用可区分 APP 大小写的既有 namespace 算法。

API 与 Worker 必须挂载同一存储。Compose 已配置共享 bind：容器路径 `ORCHESTRATOR_WORKSPACE_ROOT=/var/lib/orchestrator/workspaces`，宿主路径由 `ORCHESTRATOR_WORKSPACE_HOST_ROOT` 指定。不能把仅存在于 API 容器内的路径作为宿主 source。

直接在宿主机启动 API/Worker（不使用 Compose）时，将 `ORCHESTRATOR_WORKSPACE_ROOT` 与 `ORCHESTRATOR_WORKSPACE_HOST_ROOT` 设置成同一可写绝对目录；未指定 host root 时自动使用 root 的绝对路径。

设置 `ORCHESTRATOR_WORKSPACE_DOWNLOAD_BASE_URL` 为 Jenkins builder 可访问的 API 地址，例如 `http://10.0.0.10:18080`；正式环境应使用 HTTPS。下载使用 24 小时有效、限制 appid/build_id 的专用签名 token，经 Authorization header 传递，不放在 URL 或传 conductor access token。Jenkins token 参数使用 password 类型，脚本不打印 token。该内部下载接口不是公开匿名下载。

本地存储默认将 workspace 服务设置 `node.id` 约束，固定到当前 Docker daemon 所在 Swarm 节点，避免任务落到无文件的节点。该节点必须可调度。远程 Docker 或跨节点调度须由运维将相同共享文件系统挂到 API、Worker 和所有目标节点，并设置 `ORCHESTRATOR_WORKSPACE_SHARED_STORAGE=true`；平台不会自动复制文件到远程节点。

新构建的输入快照强制只读挂载，即使源 Compose 写 `rw`，运行容器也不能写回输入。可写运行数据必须使用构建私有 named volume；工作区外绝对 bind、external 卷和自定义共享卷驱动被拒绝。快照和 archive 在构建结束后保留，直到显式调用 `DELETE /api/v1/jenkins/build_resources/{build_id}`；该接口先移除服务和容器，核验无其他使用者后精确删除快照、archive 与私有卷，保留 APP 暂存文件和其他构建。暂存配额不含历史快照，需监控磁盘。

新构建的本地卷和挂载固定到已登记的 Swarm 存储节点。多节点或独立 Jenkins builder 的镜像清理需要运维通过 `ORCHESTRATOR_CLEANUP_DOCKER_TARGETS` 配置受控 Docker 执行端，目标身份必须与保存的 daemon/node ID 匹配。节点未知或不可达时报告部分失败，不能把单节点检查当成全量成功；本实现不是任意多租户代码的宿主安全沙箱。

Jenkins 使用独立的 `.../builds/<build_id>` 工作目录；`BUILD_ID` 参数显式映射为 `PLATFORM_BUILD_ID`，避免与 Jenkins 自带变量混淆。post 阶段记录构建专属镜像清单、移除自己的本地 tag 和检出文件，再归档资源报告；删除失败会阻止后端宣称全量清理。公共基础镜像和共享 BuildKit 缓存不属于本接口回收范围。

## 端到端验证

测试脚本需要 Go（仅用于编译静态验证程序）和 Python requests：

```bash
python3 scripts/verify_workspace_delivery.py \
  --env-file /opt/orchestrator-test/.env \
  --api http://10.32.12.110:18080
```

默认用 `FROM scratch` 避免基础镜像源干扰，真实执行上传二进制、Dockerfile、Jenkins build/push、Swarm 部署以及 HTTP 内容校验；另可用 `--base-image <nginx-image>` 验证正常基础镜像构建。脚本始终生成本地 `build.context` + `build.dockerfile`，会创建独立测试 APP，不删除现有应用或改写旧构建。

2026-10-13 测试环境实测：

以下为改造前的历史验证记录，不代表本次构建隔离/清理接口已在真实环境验收。2026-10-14 的本次开发仅完成本地测试，未部署或操作测试环境；最新验证结果见 [实施计划第 11 节](build-isolation-and-cleanup-plan.md#11-本地实现与交付说明)。

| 验证 | APP / 构建 | 结果 |
|---|---|---|
| 上传文件参与镜像构建；上传子目录只读挂载；入队后再上传不影响运行快照 | `WORKSPACE-VERIFY-8a0b169ff4d9` / `91816a6174d84f71997f777307e480d1`，Jenkins #53 | succeeded；HTTP 读取两份文件内容与本次构建标记一致 |
| （历史）image-only；整个 workspace 只读挂载；入队后再上传隔离 | `WORKSPACE-VERIFY-952f9ce570cc` / `8312599faadd4f87a00300a186deb655`，Jenkins #54 | 改造前验证记录；当前本地构建契约不再支持此模式 |

Docker service 实际 Mount.Source 位于 `/srv/orchestrator-test/workspaces/<namespace>/snapshots/<build_id>/files`，只读标志与 Compose 一致，Placement 包含存储节点的 `node.id` 约束。API 与 Worker 都挂载 `/srv/orchestrator-test/workspaces` 到 `/var/lib/orchestrator/workspaces`。

此前两次基础镜像尝试分别遇到 builder 外部镜像代理连接拒绝、Harbor token endpoint 的证书不被 BuildKit 信任。未降低 TLS 校验或修改镜像源；`FROM scratch` 的真实构建、Harbor push 和 Swarm pull 已成功。这些基础镜像拉取环境问题仍需运维单独处理，不能把 workspace 成功等同于所有外部镜像源可用。

回归结果：宿主环境 `pytest -q` 为 615 passed；实际 Python 3.12 运行镜像内为 521 passed、2 skipped（浏览器相关模块缺少可选依赖），workspace/API/Worker/Jenkins/Swarm 单元测试均执行通过。Jenkins 完整流水线 lint 与受控 Job 配置回读通过；未关闭 Groovy sandbox。
