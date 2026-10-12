# App ID 与外部资源命名迁移说明

## 身份模型

`appid` 是业务层不可变的身份键。它在应用 API、MongoDB、构建记录、事件、镜像记录、部署记录和查询 URL 中都保留用户提交的原值，包括大小写。不要把规范化后的 Docker 名称用作数据库主键、外键或查询参数；这样可以保留 `Foo` 和 `foo` 为两个不同的应用。

仓库的 `update_app` 同样拒绝改写该键，不仅依赖 HTTP PATCH 请求模型过滤字段。模板普通环境及依赖连接环境的 `{appid}` 占位符表示原始业务 ID，`{namespace}` 表示外部 namespace；`{host}` 仍表示实际服务发现名称。没有绑定应用的通用模板中，这两个身份占位符为空，待应用级配置提供具体身份。

外部系统有比业务 ID 更严格的命名约束，因此 Harbor repository、Swarm network/service、Compose 模板生成的应用前缀等位置使用同一个确定性映射 `external_namespace(appid)`：

- 如果原 ID 已经是合法的小写 Docker repository component，并且不以保留前缀 `appid-v1-` 开头，就原样作为 namespace，避免改变现有小写应用名称。
- 其他合法业务 ID 编码为 `appid-v1-` 加原始 UTF-8 字节的十六进制表示。保留前缀本身也会编码，确保原业务 ID 不会与编码输出碰撞。
- 映射只接收原始业务 `appid`，不是幂等转换；禁止把已生成的 namespace 再送入该函数。

Jenkins `APPID` 参数、HTTP/API 数据和领域模型继续使用原始值。仅外部资源路径/名称使用 namespace。Harbor repository 名称必须符合 Docker repository 规则，并在 Jenkins 侧再次校验，以便直接触发 Jenkins 的调用也能在执行 Docker 操作前失败。

## 迁移范围与兼容性

该调整不增加 MongoDB 字段、不变更集合索引、不迁移 `appid`，也不改写历史构建产物或已保存的镜像引用。合法小写且不使用保留前缀的现有应用，其 namespace、镜像 repository 和既有服务命名约定保持不变；默认 named volume 的隔离规则见下节。大写或其他需编码的应用从新版本开始使用新的独立外部 namespace。

历史上 Swarm 适配器曾对 appid 调用 `.lower()`。因此 `Foo` 和 `foo` 可能已经映射到相同的旧网络、服务名、标签或卷前缀。升级后不得仅凭旧的小写名称/label 自动认领、更新或删除旧资源；旧标签无法证明大小写不同的业务 owner。即使当前业务 appid 本身是小写，也必须核对资源上的原始 `appid` 和完整 `namespace` 两个标签，不能自动把缺少 namespace 的资源当作兼容旧资源认领。

需要延续旧部署时，应在维护窗口逐项核对已保存的原始 appid、部署记录、Docker 对象 ID 和实际服务/网络 owner，再通过显式人工迁移完成。确认归属后，可以补上相互匹配的原始 appid、完整 namespace 标签，保留可兼容的物理名称；标签本身不迁移数据。需编码的应用则使用新的编码 namespace，并显式迁移其历史部署。未经核实的旧资源会保留供核查，而非进行有破坏性的自动清理。

Compose 用户 labels 不能覆盖控制器保留的 owner 标签。更新同名 service/network 前也要检查可验证的 owner；缺少或不匹配时应 fail closed。长名称压缩必须保留完整的稳定哈希后缀，不能先截断再丢掉哈希。

部署前会核对所有逻辑服务名与物理名称的映射。例如应用 `foo` 中同时声明 `api` 和 `foo-api` 会映射到同一个兼容服务名，必须报错而不是让第二个服务覆盖第一个。不同默认卷别名的隐式物理名碰撞也会拒绝；有意共享需用显式卷声明。

## Named volume 的隔离与旧数据保护

普通 named volume 现在使用统一的 `managed_volume_name(namespace, alias)` 按应用隔离。物理名包含 `vol-v1-` 可读前缀和完整身份组合的 128 位稳定摘要，总长不超过 63 字符；namespace 和逻辑卷别名分别序列化，而非简单用连字符拼接。这样应用 `foo` 的 `bar-data` 和应用 `foo-bar` 的 `data` 不会意外合并，`Foo` 与 `foo` 以及同一应用的大小写不同卷别名也保持隔离。摘要用于抗碰撞，不是可逆身份键；业务和资源归属仍以原始 appid 与完整 namespace 为准。

未在顶层声明的 named mount 也遵守该规则；绝对路径 bind mount 仍保留显式路径。模板会先按 component/source 两个独立字段生成稳定逻辑卷别名，再应用同一个应用级映射，避免组件名与卷名的连字符边界混淆。绑定应用的模板在顶层通过 `name` 保存最终物理名称，Jenkins 构建产物保留这份声明，部署时不会再次加前缀或缩短。未绑定应用的通用模板只声明隐式卷，不声明全局共享物理名；把同一模板用于不同应用会各自隔离，且与绑定应用后生成的卷身份一致。

worker 在部署前还会核对构建产物与源 Compose 的顶层卷声明和各服务挂载；丢失或改写 `name`/`external`、卷 source/target 或挂载模式时，构建明确失败且不会调用 Swarm 部署。升级时应同时更新 Jenkins job 的流水线，否则旧任务丢失卷元数据会被消费端拒绝，而不是被当作新的隐式卷使用。

显式 `name` 或 `external: true` 仍表示操作者有意使用指定/共享的卷，不会自动添加应用前缀。升级已有服务时，如果默认卷规则会让同一个挂载点改用不同卷，部署会拒绝更新，避免静默挂载空卷。不会自动复制、重命名或删除历史卷。

需要保留历史卷时，先核对该卷的真实归属及备份，再明确声明原物理名称，例如：

```yaml
services:
  api:
    image: example/api:1
    volumes:
      - data:/data
volumes:
  data:
    name: verified-old-data
    external: true
```

`verified-old-data` 必须替换为经核实的现有卷名。如果需要迁移到新隔离卷，应在维护窗口单独迁移数据并验证，再更新部署；仅调整 label 或重复提交构建不会迁移数据。

## 构建状态提示

构建创建 API 返回 `queued` 表示任务已受理，是提交时的状态快照，不代表之后仍在排队。客户端必须继续查询 `build_info/{build_id}`，并在 `succeeded`、`failed` 或 `cancelled` 时停止轮询。诊断时以持久化构建记录、事件和 Jenkins build 结果为准；不能仅凭创建响应认定 Celery、Jenkins 队列仍在等待。
