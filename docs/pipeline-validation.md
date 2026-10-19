# Jenkins Pipeline 发布校验

Python 单元测试不能替代 Groovy/Jenkins 编译：Pipeline 中的 `sh '''...'''`
会先经过 Groovy 字符串解码，再由 shell 和 Python 执行。Python 的 raw string
前缀或 shell heredoc 并不能绕过第一层转义。

`tests/test_jenkins_pipeline.py` 检查内嵌逻辑及字符串转义规则；上线前还必须
把**整份** Pipeline 交给实际 Jenkins 的 Declarative Pipeline linter。

## 测试环境发布门禁

复用现有测试环境配置，不能为了发布重新生成 `.env`、加密密钥或数据库：

```bash
python3 scripts/jenkins_pipeline_gate.py --env-file /opt/orchestrator-test/.env
```

默认只校验，不修改 job。确认目标 job 与现有配置匹配后，可校验并更新：

```bash
python3 scripts/jenkins_pipeline_gate.py --env-file /opt/orchestrator-test/.env --apply
```

流水线新增的 `BUILD_ID`、`EXPECTED_COMMIT`、`WORKSPACE_ARCHIVE_URL`、`WORKSPACE_ARCHIVE_SHA256`、`WORKSPACE_ARCHIVE_TOKEN` 和 `CHECKOUT_REQUIRED` 参数会由 gate 在缺失时声明，token 为 password 类型；其余 Job 设置保持不变。这样首次构建不会因 Jenkins 丢弃未声明参数而丢失平台构建标识、固定提交或上传文件，也不会要求仅上传文件的 APP 配置 GitLab 凭据。

该脚本：

- 从现有配置读取认证信息，不把凭据作为命令行参数或输出。
- 只允许固定的测试 job，且要求现有配置匹配；不提供改指其他 job 的参数。
- 仅把 `Jenkinsfile successfully validated.` 作为校验通过。
- 将 HTTP 200 中的编译错误、`No Jenkinsfile specified` 或登录页视为失败。
- 校验失败时不写 job；成功后只替换现有内联 script，并保留 sandbox 等配置。
- 更新后回读 `config.xml`，检查 script 完全一致、sandbox 和其他配置保持不变（忽略 XML 排版空白）。

门禁默认使用 `apps-orchestrator/orchestrator-real-delivery-20260920`。不要将它
指向生产 job，也不要在校验失败时绕过门禁手工发布。

## 真实链路验收

更新测试 API、worker 和 beat 后，还需真实提交一个唯一测试应用并验证：

1. 创建构建返回 HTTP 202，仅代表受理。
2. 自动查询构建状态，直到 `succeeded`、`failed` 或 `cancelled`。
3. Jenkins 构建成功、归档 `orchestrator-result.json`、镜像进入 Harbor。
4. Swarm service ready、挂载与镜像身份正确；配置了端口时检查外部 HTTP。
5. 终态后停止查询；构建详情与历史记录一致。

浏览器验收脚本要求显式提供该环境允许的仓库和引用；无效的示例仓库不能
被当作流水线失败：

```bash
python3 scripts/verify_namespace_webui.py \
  --repository-url https://gitlab.example/group/app.git \
  --git-ref main \
  --run PIPELINE-RELEASE
```

该脚本会创建隔离测试记录和资源，不会清理原 APP 或修改旧构建记录。

## 应用源码与开发 Compose

平台只接受本地源码构建输入：每个服务必须配置 `build.context` 和
`build.dockerfile`，并且 Dockerfile 必须真实存在于对应构建上下文内。image-only
服务不再属于用户 Compose 合同，不能用现成镜像代替源码构建。Dockerfile 应负责
把应用代码和依赖打包进该服务的运行镜像；workspace API 上传的文件可以作为构建输入，
也可以在确有运行时读取需求时通过显式 `.:/workspace` 挂载本次构建快照。

Worker 在触发 Jenkins 前解析并验证 workspace 相对挂载；没有上传源文件、路径越界或其他无法
表示为 Swarm service 的配置明确失败，不会静默删除挂载。
修改应用仓库的 Compose 后，还需同步更新 APP 保存的 Compose。平台代码
发布不会自动改写这些应用数据，也不会把历史失败构建改成成功。
