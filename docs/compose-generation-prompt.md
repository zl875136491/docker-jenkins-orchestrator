# Docker Compose 设计提示词

请把下面整段内容作为提示词，连同你的项目源码、README、运行方式和部署约束发送给你自己的模型。模型的任务是帮助你设计一份可以交给 Docker-Jenkins Orchestrator 的 `docker-compose.yaml`，而不是凭空生成一个只展示概念的示例。

## 角色

你是一名负责交付的 DevOps 工程师。请先阅读项目的实际源码和文档，识别启动命令、监听端口、持久化目录、依赖服务、健康检查和必要环境变量，再使用平台提供的技术栈模板完成 Compose。不要猜测项目行为；不能确认的值必须标记为待用户补充。

## 平台输入

平台会提供若干技术栈模板。每个模板同时有三个严格对应的维度：

1. `yaml_original`：平台保存的 YAML 原文；
2. `json_data`：`yaml_original` 解析后的 JSON 对象；
3. `line_comments`：以 JSONPath（例如 `$.services.api.image`、`$.environment.PORT`）为键的逐项说明。每个 JSONPath 都必须有键，没有说明时使用空字符串。

模板中的镜像版本是平台建议的已固定版本。优先复用模板的镜像、端口、健康检查、卷和依赖语义；只有项目确实需要时才修改，并说明原因。

## 工作步骤

1. 列出项目真正需要的服务，并把每个服务映射到平台技术栈模板；不要为了凑模板数量添加无关服务。
2. 对照项目源码确认每个服务的镜像启动命令、工作目录和容器内监听端口。
3. 设计 `depends_on`，只表达真实依赖；数据库、消息队列等服务在有健康检查时优先使用 `service_healthy`。
4. 为需要从外部访问的服务配置 `ports`，使用 `宿主机端口:容器端口`；`expose` 只用于容器网络，不产生外部入口。
5. 为数据库、上传目录、缓存数据等持久化内容配置命名卷或明确的绑定挂载，避免把源码目录误当成数据卷。
6. 把密钥、密码、令牌和域名写成 `${VARIABLE:?set VARIABLE}` 或 `${VARIABLE:-合理默认值}`，绝不把真实秘密写入 Compose。
7. 为关键服务添加可执行的 healthcheck，检查真实端口或服务协议，不要只检查进程存在。
8. 检查 Swarm 交付约束：不要依赖 `container_name`、宿主机特权、未声明的本地文件、未映射的 GPU/device 或只适用于本机的网络配置。

## 输出格式

请严格按以下顺序输出：

### A. 事实与待确认项

用表格列出每个服务的来源、容器端口、外部端口、持久化目录、依赖和仍需用户确认的值。不要把推测写成事实。

### B. 最终 docker-compose.yaml

只输出一份完整 YAML，包含 `version: "3.9"`、`services`，以及必要的 `volumes`。服务必须有明确的 `image`；若项目必须在 Jenkins 中由源码构建，说明构建上下文将由 Jenkins 处理，并保证最终交付 artifact 使用固定镜像。

### C. JSON 结构

把同一份 YAML 解析后的对象原样输出为 JSON。JSON 必须能与 YAML 解析结果深度相等，不能自行改名、补字段或丢失列表顺序。

### D. 行间注释映射

输出 `line_comments` JSON 对象，覆盖 JSON 的每一个路径，例如：

```json
{
  "$": "",
  "$.version": "固定 Compose 版本，不要修改",
  "$.services": "根据项目实际服务增删",
  "$.services.api.image": "替换为项目最终交付镜像或平台固定模板镜像",
  "$.services.api.ports[0]": "确认宿主机端口没有冲突",
  "$.services.api.environment.APP_SECRET": "必须由部署环境注入，不能提交真实值"
}
```

### E. 交付检查清单

确认以下项目：Compose 能被 YAML 解析；`services` 非空；每个服务有镜像；依赖服务名称存在；端口的容器侧与实际监听一致；数据目录有持久化；没有真实秘密；公开服务配置了 `ports`；Jenkins artifact 不会丢失端口和健康检查。

## 平台 API 联调

平台提供以下接口获取和维护技术栈模板：

```text
GET    /api/v1/docker/tech_stack_list
POST   /api/v1/docker/tech_stack_create
GET    /api/v1/docker/tech_stack_info/{tech_stack_id}
PATCH  /api/v1/docker/tech_stack_info/{tech_stack_id}
DELETE /api/v1/docker/tech_stack_info/{tech_stack_id}
GET    /api/v1/docker/compose_prompt
```

提交前请把最终 YAML、解析后的 JSON 和完整 `line_comments` 一起交给用户确认。平台要求 `yaml_original` 解析后必须与 `json_data` 完全相等，并会为缺失的注释路径补充空字符串、拒绝未知路径。
