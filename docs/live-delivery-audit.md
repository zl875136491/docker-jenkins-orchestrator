# 真实联调交付审计

本报告记录一次经授权、隔离命名空间的真实交付验收。认证信息来自项目根目录的
`auth.txt`，但没有写入仓库、结果文件或本报告。

## 环境

- Jenkins：`http://10.17.158.156`
- Harbor Registry/API：`https://10.17.158.118`
- Jenkins folder：`apps-orchestrator`
- Harbor project：`apps-orchestrator`
- Docker Engine：本机单节点 Swarm manager，Docker 28.0.1
- MongoDB：本机认证连接，每次运行使用唯一临时数据库
- Redis：本机认证连接，使用 Redis DB 15 和唯一 Celery queue 名称
- DNS 备注：`jenkins.1oa.com.cn`、`harbor.1oa.com.cn` 在联调主机上不可解析，因此使用已验证的内网地址

Jenkins 临时 job `live-e2e-final-1789363209` 在验收结束后已删除；此前诊断使用的
`live-e2e-742655080523` 也已删除。

## 保留证据的一轮实机复验

为核对交付链路在 Jenkins 和 Harbor 上确实留下可检查的痕迹，另外执行了一轮
Linkwarden，并显式使用 `--retain-evidence`。本轮的应用标识符合
`app-name + UUID` 约定：
`linkwarden-8f8873660a2444ada4e0906135813e48`。

- 运行标识：`evidence-20260914150228-e4beec90`
- Jenkins job：[live-e2e-evidence-1789368954](http://10.17.158.156/job/apps-orchestrator/job/live-e2e-evidence-1789368954/)
- Jenkins build：[#1](http://10.17.158.156/job/apps-orchestrator/job/live-e2e-evidence-1789368954/1/)
- Jenkins 制品：[orchestrator-result.json](http://10.17.158.156/job/apps-orchestrator/job/live-e2e-evidence-1789368954/1/artifact/orchestrator-result.json)
- Harbor 仓库详情（管理员 API）：
  `https://10.17.158.118/api/v2.0/projects/apps-orchestrator/repositories/linkwarden-8f8873660a2444ada4e0906135813e48`
- Harbor 仓库页面：
  `https://10.17.158.118/harbor/projects/9/repositories/apps-orchestrator%252Flinkwarden-8f8873660a2444ada4e0906135813e48/artifacts-tab`
- Harbor registry 标签接口：
  `https://10.17.158.118/v2/apps-orchestrator/linkwarden-8f8873660a2444ada4e0906135813e48/tags/list`

本轮构建结果为 Jenkins `SUCCESS`，制品包含 `linkwarden`、
`linkwarden-meilisearch`、`linkwarden-postgres` 三个服务；Harbor 保留了对应的三个
标签：`1-linkwarden`、`1-linkwarden-meilisearch`、`1-linkwarden-postgres`。三个镜像的
manifest digest 均为 `sha256:a81e7e3358fc362a16eaabb874be9b382d90dc1c64f4737d3cdef5dec9806f18`。

本轮 Jenkins job 是隔离联调用的交付合同 fixture：它将已验证的构建源镜像重新标记并推送
到本轮 Harbor 仓库，再生成包含 Linkwarden 三服务拓扑的规范化 artifact（每个 service 使用
长驻测试命令）。因此本轮真实验证的是 FastAPI -> Celery/Redis -> Jenkins -> Harbor ->
Swarm -> Mongo 的交付链路、参数传递和资源生命周期；不应将其解读为 Linkwarden 源码编译、
业务页面可用性或生产镜像供应链验收。生产环境仍需配置实际的源码构建 Jenkins job，并增加
部署后的应用健康检查。

保留范围仅限上述 Jenkins job/build/artifact 和 Harbor repository/tag。验证结束后已
删除本轮 Mongo 临时数据库、Redis DB 15 中的运行专属 key、Swarm service/network/image
以及本机 API、Celery worker 和 Redis monitor；独立复查结果分别为 Mongo 不存在、Redis
`dbsize=0`、运行专属 key 为 0、Swarm 匹配资源为 0。

## 三轮正式结果

运行标识：`final3-1789363218`

| 项目 | appid | build_id | Jenkins build | 服务数 | 任务状态 | 事件数 |
| --- | --- | --- | ---: | ---: | --- | ---: |
| Umami | `umami-7a02187443af4ba681e8c99796481126` | `835e2444baa442b8abbbb9b27a056cba` | 1 | 2 | `queued -> triggering -> building -> succeeded` | 12 |
| Planka | `planka-5472cb380b34481a827605cb94b43e5f` | `deae21d5f0d44cb2ae105297dc456f3c` | 2 | 2 | `queued -> validating -> building -> succeeded` | 12 |
| Linkwarden | `linkwarden-83c25376fefa4750972d3cdd6ef43d10` | `fb83dfa8d42b4b75a3e9a3d506769ff7` | 3 | 3 | `queued -> triggering -> building -> succeeded` | 12 |

每一轮均验证了：

- FastAPI `/api/connect`、JWT Bearer 认证和 `/api/me`；
- Mongo 中 `user_apps=1`、`build_jobs=1`、`deployment_services=服务数`、`alerts=0`；
- Celery worker 从 Redis 接收 `start_build`/`poll_build`，Redis MONITOR 观察到本轮独有 queue 的投递和消费；
- Jenkins build 完成且 `orchestrator-result.json` artifact 存在，artifact 服务拓扑与用户 Compose 一致；
- Harbor 每个服务都有 tag 和 `sha256` manifest digest；
- Swarm service 数量、appid label、临时 overlay network 和所有 task 均正确，task 最终为 `running`；
- 清理后 Mongo 相关集合计数全部为 0，Harbor repository 删除成功，Jenkins build 删除成功，Redis 本轮 key 删除完成。

本轮 Redis 观测共 301 条相关 MONITOR 记录，其中 59 条 enqueue、161 条 consume。
Redis 仅作为 Celery broker，业务状态仍由 Mongo 持久化。

## 失败定位与修复

### Compose 端口丢失定位（2026-09-20）

`test-demo-1` 的控制端记录和 Jenkins 入参均包含 `ports: ["18082:3000"]`，但旧的
`live-e2e-evidence-1789368954` job 在生成 `orchestrator-result.json` 时重建 service，
只保留 image、长驻 command 和禁用 healthcheck，主动丢弃了 ports、environment、restart
等字段。因此 Swarm service 的 `Endpoint.Ports` 为 `null`；源 `compose.yaml` 没有问题。

修复包含两层保护：标准流水线模板直接复制源 service 定义并仅替换最终 image，Verify 阶段
校验 service 集合和每个端口映射；worker 在部署前再次比较源 Compose 与 Jenkins artifact，
拓扑或端口被改动时构建失败且不创建 Swarm service。无 build context 的 image-only 服务
采用一次真实 pull 后 tag/push，避免受限 builder 网络中 `docker build --pull` 的二次拉取。

保留证据的一次实机复验使用 appid
`test-real-nginx-0cc696b9d3f3`、build id `ee778e50e1184fe89cdf0dff2f35dd16` 和
Jenkins job `orchestrator-real-delivery-20260920` #5。Jenkins artifact、Harbor tag
`5-web`、Swarm endpoint `18083:80` 均存在，`GET http://10.32.12.110:18083/` 返回
Nginx `200 OK`；控制端 `/api/apps/{appid}/access` 返回
`http://10.32.12.110:18083`。

随后用同一 app 触发 Jenkins #6（build id
`2d1c2b307f9144c6b16f9ebbc7b74f97`）复验新的运行态闸门：worker 等待 Swarm task
进入 `running` 并从 worker 网络探测 `10.32.12.110:18083` 后才写入 `succeeded`；
Jenkins #6、Harbor `6-web`、Swarm task 和外部 Nginx `200 OK` 均通过。

### 原 `test-demo-1` 应用复验

原 app 的 Compose 虽然声明了 `18082:3000`，但只有裸 `node` 基础镜像，没有任何监听
3000 的 command。使用实际分支 `master` 触发的 Jenkins #9 成功生成并推送了镜像，但
worker 的公开端口探测返回 `ConnectionRefusedError`，构建被标记为 `failed`，没有伪造
部署成功。随后通过控制端更新 app 的 git ref 为 `master`，并补充最小 Node HTTP server
command，再触发 Jenkins #10（build id `dd31c99775544a849ceb0aae0835c601`）。

本轮验证结果：Jenkins #10 `SUCCESS`，artifact 保留 `ports: ["18082:3000"]` 和
command，Harbor 镜像为
`10.17.158.118/apps-orchestrator/test-demo-1:10-react`，Swarm task 为 `Running`，
`GET http://10.32.12.110:18082/` 返回 `200 OK`，控制端
`/api/apps/test-demo-1/access` 返回 `http://10.32.12.110:18082`。

### 历史服务记录与 Compose 端口诊断（2026-09-21）

控制端中 `test-demo-1` 的源 Compose 和当前 Jenkins 入参均包含
`ports: ["18082:3000"]`。历史 build `b9d8a21058e24ef1b7fc938bf79542bf` 和
`4550d148debc4c79ba951619540b8973` 的 `deployment_services` 文档是在端口保真修复前写入
的快照，因此仍显示 `endpoint: null`；它们不是当前 Swarm service 的端口状态。当前 build
`dd31c99775544a849ceb0aae0835c601` 的记录包含 `published_port: 18082`，Swarm
`Endpoint.Ports` 为 `18082->3000`，外部 GET 返回 `200 OK`。

修复后的 Planka WebUI 复验使用 appid
`planka-1c4a17ed51fb402b9797a19f5eb650aa`、build
`91294f2c69364556bac9331be451d95d` 和 Jenkins #15。两项 Swarm service/task 均为
`1/1 Running`，应用入口 `http://10.32.12.110:18086/` 返回 `200 OK`，数据库 service
使用 Compose 内部 alias `planka-db` 正常连接。部署失败回滚测试同时确认，readiness 失败
只清理本次新建 service/network，不删除已有应用资源。

此前 Linkwarden 轮次使用完整 UUID appid 时，Jenkins、Harbor 和 Celery 均成功，Swarm
在创建 `linkwarden-meilisearch` 时返回：

```text
rpc error: code = InvalidArgument desc = name must be 63 characters or fewer
```

原因是原始 `appid-compose_service` 名称超过 Docker Swarm 63 字符限制。适配器现在对
超长 network/service 名称使用稳定的 SHA-256 短哈希，短名称保持原有可读格式；并将 Docker
API 的安全错误摘要写入构建错误，避免只显示“Unable to deploy Docker service”。修复后的
单独 Linkwarden 复验和正式三轮均成功。

## 最终清理核验

联调脚本退出后再次直接查询外部系统，结果如下：

- Jenkins 两个临时 job：HTTP 404；
- Harbor `apps-orchestrator` repository 列表：空；
- Mongo：无 `orchestrator_live_*` 数据库；
- Redis DB 15：`dbsize=0`，无 key；
- Docker Swarm：无本轮 appid service、network 或 task；
- 本机临时 API、Celery worker、Redis monitor：全部退出。

诊断脚本和结果文件只保留在 `/tmp`，未提交到 Git；其中包含的 appid、build id 和
基础设施状态不含密码、token 或环境变量值。

## 可复现命令

```text
/tmp/orchestrator-live-venv/bin/python scripts/live_delivery.py \
  --job <temporary-job> \
  --projects umami,planka,linkwarden \
  --run <unique-run> \
  --result /tmp/<unique-run>-results.json
```

脚本在 Redis DB 15 非空时会拒绝启动；每个 appid 在创建后立即登记清理信息，失败轮次
也会清理已经创建的 Mongo、Jenkins、Harbor、Swarm 和 Redis 资源。
