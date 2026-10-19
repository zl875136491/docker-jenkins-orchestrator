# Docker Compose 设计提示词

请把下面整段内容作为提示词，连同你的项目源码、README、运行方式和部署约束发送给你自己的模型。模型的任务是帮助你设计一份可以交给 Docker-Jenkins Orchestrator 的 `docker-compose.yaml`，而不是凭空生成一个只展示概念的示例。

## 角色

你是一名负责交付的 DevOps 工程师。请先阅读项目的实际源码和文档，识别启动命令、监听端口、持久化目录、依赖服务、健康检查和必要环境变量，再使用平台提供的技术栈模板完成 Compose。不要猜测项目行为；不能确认的值必须标记为待用户补充。

## 平台输入

平台会提供若干技术栈模板。每个模板同时有三个严格对应的维度：

1. `yaml_original`：平台保存的带逐路径注释 YAML；注释是适配提示，不属于数据；
2. `json_data`：`yaml_original` 解析后的 JSON 对象；
3. `line_comments`：以 JSONPath（例如 `$.services.api.image`、`$.environment.PORT`）为键的逐项说明。优先结合 YAML 中的注释与映射值适配项目；若两种说明同时存在，以 `line_comments` 为准。

模板中的镜像版本是 Dockerfile 可使用的已固定基础镜像候选。优先复用模板的镜像、端口、健康检查、卷和依赖语义；只有项目确实需要时才修改，并说明原因。

## 强制 Compose 构建约束

1. Compose 必须是**本地源码构建模式**；每个 `services.<service>` 都必须包含 `build` 对象，禁止 image-only 服务。
2. 每个服务的 `build` 必须同时包含相对路径 `context` 和相对路径 `dockerfile`，例如：

   ```yaml
   services:
     api:
       build:
         context: .
         dockerfile: Dockerfile
   ```

3. `dockerfile` 指向的文件必须真实存在于该服务的 build context 内；不能省略、依赖平台自动补全 `Dockerfile`、使用绝对路径或通过 `..` 跳出上下文。
4. 每个服务都要把自身运行所需的源码、依赖声明和 Dockerfile 放在仓库或上传 workspace 中；不要只提供基础镜像名来代替应用镜像构建。
5. `image` 可以作为本地构建标签或基础模板提示保留，但不能替代 `build`，平台最终会用当前 build 的不可变镜像替换它。

## 工作步骤

1. 列出项目真正需要的服务，并把每个服务映射到平台技术栈模板；不要为了凑模板数量添加无关服务。
2. 对照项目源码确认每个服务的镜像启动命令、工作目录和容器内监听端口。
3. 设计 `depends_on`，只表达真实依赖；数据库、消息队列等服务在有健康检查时优先使用 `service_healthy`。
4. 为需要从外部访问的服务配置 `ports`，使用 `宿主机端口:容器端口`；`expose` 只用于容器网络，不产生外部入口。宿主机 published 端口只是申请值，内部 worker 会统一检查并重新分配以避免项目之间冲突，最终交付端口可能与 YAML 不同；冒号右侧的容器 target 端口必须与应用实际监听一致。
5. 为数据库、上传目录、缓存数据等持久化内容配置命名卷。语言模板的 `.:/workspace` 引用 APP 通过 workspace 接口上传、在创建构建时冻结的文件，不是 Jenkins 的 Git checkout。`./config/file:/app/config:ro` 可挂单个上传文件；不允许 `..`、`~` 或缺失源路径。应用源码应优先由每个服务自己的 Dockerfile 复制进镜像；只有确实需要运行时读取上传文件时才声明 workspace 挂载。仅上传文件构建时设置顶层 `x-orchestrator-build-source: workspace`，并为每个服务提供 Dockerfile 和 build；默认 Git 模式把上传文件覆盖到独立构建目录。
6. 把密钥、密码、令牌和域名写成 `${VARIABLE:?set VARIABLE}` 或 `${VARIABLE:-合理默认值}`，绝不把真实秘密写入 Compose。
7. 为关键服务添加可执行的 healthcheck，检查真实端口或服务协议，不要只检查进程存在。
8. 检查 Swarm 交付约束：不要依赖 `container_name`、宿主机特权、未声明的本地文件、未映射的 GPU/device 或只适用于本机的网络配置。
9. 以 build 为资源隔离边界：可写数据只使用构建私有 named volume；上传 workspace 输入会强制只读。不要提交工作区外绝对 bind、external 卷/网络或共享存储驱动。显式卷 `name` 不能用于跨版本共享数据，平台仍会重新映射。
10. 数据库、Redis、对象存储等状态型后端应作为同一构建内的独立服务，通过 Compose service 名连接；不要复用另一个构建或外部共享数据库/桶。常见连接变量无法证明目标独立时会被阻塞。不要尝试用改名或忽略校验绕过共享数据风险。

## 输出格式

请严格按以下顺序输出：

### A. 事实与待确认项

用表格列出每个服务的来源、容器端口、外部端口、持久化目录、依赖和仍需用户确认的值。不要把推测写成事实。

### B. 最终 docker-compose.yaml

只输出一份完整 YAML，包含 `version: "3.9"`、`services`，以及必要的 `volumes`。每个服务必须有 `build.context` 和 `build.dockerfile`；每个 Dockerfile 必须位于对应 context 内并负责生成该服务的运行镜像。`image` 如保留只能作为本地构建标签/模板提示，不能单独出现，也不能替代 Dockerfile 构建。

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
  "$.services.api.ports[0]": "宿主机 published 端口仅为申请值，内部 worker 会重新检查和分配；确认冒号右侧容器端口与应用监听一致",
  "$.services.api.environment.APP_SECRET": "必须由部署环境注入，不能提交真实值"
}
```

### E. 交付检查清单

确认以下项目：Compose 能被 YAML 解析；`services` 非空；每个服务都有 `build.context`、`build.dockerfile` 且 Dockerfile 真实存在；没有 image-only 服务；依赖服务名称存在；端口的容器侧与实际监听一致；数据目录有持久化；没有真实秘密；公开服务配置了 `ports`；Jenkins artifact 不会丢失端口和健康检查。

## 内部 worker 输出交付材料

你的输出将作为内部 worker 的输入，不需要调用或描述任何平台 API。请一次性交付以下材料：事实与待确认项、最终 `docker-compose.yaml`、与 YAML 深度相等的 JSON、覆盖每个 JSONPath 的完整 `line_comments`，以及交付检查清单。

worker 会校验 YAML 与 JSON 是否完全一致、注释路径是否属于最终 JSON，并将这些材料用于后续构建和部署。提交前明确标出仍需人工确认的镜像、端口、环境变量和持久化路径；不要声称已经执行构建、访问外部服务或完成部署验证。
