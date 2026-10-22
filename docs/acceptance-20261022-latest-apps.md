# 10.32.12.110 最新应用数据在 10.32.12.111 的验收记录

日期：2026-10-22（UTC+08:00）。最终综合执行时间：09:18:58–09:19:05。

结论：**接口与数据断言 142/142 通过；初次浏览器验收发现的卡片与窄屏导航问题已修复并通过 UI 回归；真实构建与部署仍未验证。**
综合报告 `142/143` 是修复前的结果：142 项接口/数据断言通过，浏览器布局断言失败。

## 样本与隔离

源环境 `10.32.12.110:18080` 共返回 34 个应用，按 `updated_at` 降序选择最近三个。
目标环境是 `10.32.12.111:8088`，应用服务部署目录 `/opt/orchestrator`。

| 源应用 | 源更新时间（UTC+08:00） | 分支 | 目标测试 App ID |
| --- | --- | --- | --- |
| `APP-20261021-151B539F`：验证APP-Nginx路由支持 | 2026-10-21 10:58:14.565 | `main` | `TEST110-20261022-151B539F` |
| `APP-20261016-FF053C01`：orderapp | 2026-10-16 16:54:36.960 | `develop` | `TEST110-20261022-FF053C01` |
| `APP-20261016-19596276`：dockerfile部署验证 | 2026-10-16 11:50:21.373 | `develop` | `TEST110-20261022-19596276` |

导入仓库地址、分支、Compose、components 等应用配置，名称统一增加 `[110测试]`。
三个源样本均没有环境变量密钥、workspace 文件或 `require_route`。
使用独立 ID，不覆盖源 ID；重复执行仅复用配置一致的测试应用。

没有迁移第三方集成凭据、workspace 内容、旧构建状态、服务 ID 或成功状态。
公共创建 API 不接收源时间和 Compose 来源元数据，因此没有伪装保留源
`created_at`、`updated_at`、`compose_source_file`、`compose_source_commit`；来源关系和 SHA 保存在报告中。
源环境只执行登录取 token 和 GET，源样本配置及最新构建摘要在接口测试前后的快照一致。

## 接口与数据测试：142 项通过

- 使用目标真实登录接口获取 token，检查健康接口及应用列表。
- 导入/读取配置一致，重复 App ID 返回 409；测试改名并恢复原名称。
- 非法路由被拒绝；缺少 GitLab 配置时更新路由返回 422，拒绝的更新没有落库。
- 构建历史按应用过滤、分页和状态查询；没有导入源成功记录。
- 三个旧应用缺少 `require_route`，直接构建返回 422：
  `未进行路由静态检查, 无法交付构建任务`。
- 静态检查提交缺少路由返回 422；SHA 查询/校验以及不存在构建的动态检查返回符合预期。
- Workspace 上传小型测试标记，校验文件大小和 SHA256，然后仅删除该标记；复查为空。
- events、images、services、alerts、access 没有混入源环境数据。
- 校验两种路由检查角色可读，样本仍在列表中，其他已有应用未变化。

以上构建/检查请求只验证前置条件拒绝行为，没有派发真正的构建、模型调用或部署任务。

## 真实浏览器：确认卡片布局缺陷

使用全新 Chromium 浏览器上下文、真实 `.111` UI 和 API，没有 mock。
浏览器侧只允许登录/刷新和白名单 GET，拦截其他业务写请求及跨源请求。
没有使用强制点击或 DOM 点击绕过可点击性问题。

首轮中，Nginx 和 orderapp 两个样本的详情字段、四项应用导航、构建/检查页、
重新登录弹窗关闭后的上下文、390px 移动布局、应用内历史/workspace/服务入口和返回列表检查通过。
dockerfile 样本在打开应用时超时，尚未进入详情。

定位后增加了按钮几何、命中测试和正常 actionability 检查，最终报告准确记录三个卡片都存在按钮溢出；
dockerfile 样本的按钮被 orderapp 卡片遮挡，无法正常点击。

1440px 视口下的证据：

| 目标 App ID 后缀 | 卡片横向范围 | 按钮横向范围 | 按钮中心接收点击 |
| --- | --- | --- | --- |
| `151B539F` | 1042–1382 | 1360–1436 | 是，但超出本卡片 |
| `FF053C01` | 690–1030 | 1003–1079 | 是，但超出本卡片 |
| `19596276` | 338–678 | 714–790 | 否，命中相邻 `FF053C01` 卡片 |

`.app-card` 使用 flex，文字容器计算样式为 `min-width: auto; flex: 0 1 auto`，
仓库文本为 `white-space: nowrap`。文字容器不能在卡片内适当收缩，按钮因此被挤出。
相关样式位于 `webui/styles.css:300`，卡片、仓库行和按钮规则分别从 301、304、305 行开始。

最终修复前的浏览器执行没有 HTTP 错误、网络失败、页面异常或被安全保护拦截的写请求。

## UI 修复复验

2026-10-22 已修复上述布局问题：卡片详情使用可收缩的 flex 子项，仓库地址最大宽度限制在卡片内容区；
窄屏应用工作区导航明确占满一行，操作子菜单自适应为多列，避免六项菜单独占纵向长列表。
浏览器回归覆盖三张长内容卡片在 1440px 和 390px 下的按钮边界及真实可点击性，
以及 390px、652px 下操作导航的多列布局和页面无横向溢出。

- 定向浏览器测试：4 项通过。
- 完整 UI 测试：`tests/test_webui.py` 与 `tests/test_webui_browser.py` 共 102 项通过。
- 修复后再次运行定向浏览器测试：4 项通过；`git diff --check` 通过。
- 用户随后要求部署。已仅将 `webui/app.js`、`webui/styles.css` 热更新到 `.111` 的 API 容器 `/app/webui/`；远端 SHA256 与本地匹配，未重建或重启任何容器。
- 部署后以真实 Chromium 检查 `.111`：三个样本均通过按钮边界/指针可达性、详情/构建/检查/操作导航及移动布局；无 HTTP 错误、网络失败、页面异常或被拦截写请求。
- 为使修复在容器重建后仍保留，已将 `/opt/orchestrator/docker-compose.yml` 的 API 服务配置为只读挂载 `./webui:/app/webui:ro`；先备份原 compose 到 `.111:/tmp/orchestrator-compose-backup.3Xd7M7/docker-compose.yml`，校验配置后仅重新创建 API 容器，没有重建 worker、beat 或数据服务。
- API 在重建后恢复 healthy，`/healthz` 返回 200；五个服务容器均 healthy。重建后的真实 Chromium 验收仍为三个样本全通过。
- 首次热更新验收摘要：`var/acceptance-20261022-latest-apps/deployed-ui/imported-apps-webui.json`；重建后验收摘要：`var/acceptance-20261022-latest-apps/deployed-ui-after-recreate/imported-apps-webui.json`，截图为同目录 `imported-apps-webui.png`。

## 环境限制与测试后状态

- 目标使用 Mongo 存储、Celery 派发器；api、worker、beat、mongo、redis 五个容器均为 healthy。
- GitLab URL/Token 未配置；Jenkins/Harbor 地址被配置检查标记为占位或本地地址，未验证可用性；Harbor 凭据未配置。
- Docker Swarm 状态为 `inactive`，没有擅自初始化。
- 三个样本 `require_route` 均为空。继续真实交付需要确认期望路由，补齐 GitLab、模型/检查、构建和镜像仓库等必要配置，
  并按实际部署方案配置运行环境。此次没有从源环境复制相关密钥。
- 目标保留三个 `[110测试]` 应用供页面查看；各样本构建总数、workspace 文件数、服务数均为 0，
  `access_available` 为 false。测试上传标记已删除，可以通过样本来源信息重新生成。
- 没有重启服务，也没有覆盖工作区已有未提交修改。

## 证据与复跑

路径均相对仓库根目录：

- `var/acceptance-20261022-latest-apps/report.json`：首轮接口报告，142/142。
- `var/acceptance-20261022-latest-apps/with-browser/report.json`：首轮综合报告，包含前两个样本的页面内容检查结果。
- `var/acceptance-20261022-latest-apps/ui-layout-diagnostic/imported-apps-webui.json`：独立布局定位。
- `var/acceptance-20261022-latest-apps/final/report.json`：最终综合报告，状态 `failed`、142/143。
- `var/acceptance-20261022-latest-apps/final/browser/imported-apps-webui.json`：最终浏览器明细。
- `var/acceptance-20261022-latest-apps/final/browser/imported-apps-webui.png`：已遮罩仓库地址的现场截图。
- `webui/styles.css`、`webui/app.js` 与 `tests/test_webui_browser.py`：本次 UI 修复及回归覆盖。

复跑需当前机器保留两台主机的免密 SSH、Python requests、Playwright 及 Chromium。
工具从 API 容器中读取已有凭据并仅在内存中使用，不要求也不应把凭据写进命令行或报告。

```bash
python3 scripts/verify_remote_app_samples.py --with-browser \
  --output-dir var/acceptance-20261022-latest-apps/rerun
```

已完成两个新增验收脚本的 `py_compile` 和工作区 `git diff --check`。
本次没有图谱 MCP 工具可用，源码定位采用精确文件读取，不声明图谱覆盖核验完成。
