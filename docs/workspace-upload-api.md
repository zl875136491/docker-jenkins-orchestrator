# Workspace 文件上传接口对接说明

整理日期：2026-10-15。本文以本次已核对的测试环境部署实现及配额为基准，仅说明文件上传接口，不代表构建、部署接口的状态定义。

## 1. 核心约定

- 上传为同步操作：成功返回 HTTP `201` 和文件信息，不需要轮询上传任务。
- 接口当前没有统一的业务 `status`、`code` 或 `msg` 字段。
- 受控错误通过 HTTP 状态码分类，原因放在 JSON 的 `detail` 字段中。
- 不返回构建流程中的 `queued`、`building`、`failed` 等状态。
- 上传成功仅表示文件已保存，不表示源码完整、挂载正确、构建成功或部署成功，也不会自动触发构建。

## 2. 请求格式

### 2.1 地址及前置条件

```http
POST /api/v1/workspace/{appid}
```

兼容入口：`POST /workspace/{appid}`，行为相同。

前置条件：APP 已注册，调用方持有有效的 conductor access token。`appid` 在 URL 路径中传递，不在请求体中传递。

### 2.2 请求头

```http
Authorization: Bearer <access_token>
Content-Type: multipart/form-data; boundary=...
```

请求体不是 JSON。使用浏览器 `FormData` 或 `curl -F` 时，让客户端自动生成包含 boundary 的 `Content-Type`，不要自行设置一个缺少 boundary 的请求头。

### 2.3 请求体字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `files` | 文件，可重复 | 至少一个文件 | 推荐字段名；每个文件的 `filename` 是目标相对路径，例如 `package.json`、`src/main.tsx` |
| `file` | 文件 | 兼容字段 | 与 `files` 兼容，新对接建议统一使用 `files` |
| `path` | 文本字符串 | 否 | 为本次所有文件增加相对目录前缀；不传则保存在该 APP 的 workspace 根目录下 |

例如，`path=frontend`、`filename=src/main.tsx`，最终保存路径为 `frontend/src/main.tsx`。

约束：

- 多文件使用多个同名 `files` 字段，不使用 `files[]`。
- 文件名允许包含子目录，但必须是相对 POSIX 路径。
- 同路径文件覆盖，未包含在本次请求中的其他文件保留。
- 不自动解压 ZIP/TAR；上传压缩包只会保存压缩包本身。
- 不额外提交 `appid`、`status`、`msg` 等未定义的表单字段。

### 2.4 请求示例

```bash
curl -X POST "$API/api/v1/workspace/$APPID" \
  -H "Authorization: Bearer $TOKEN" \
  -F 'files=@./package.json;filename=package.json' \
  -F 'files=@./package-lock.json;filename=package-lock.json' \
  -F 'files=@./index.html;filename=index.html' \
  -F 'files=@./src/main.tsx;filename=src/main.tsx'
```

如需统一增加目录前缀，在同一请求中额外增加：

```bash
-F 'path=frontend'
```

上述示例用于说明字段格式；实际应上传项目所需的完整文件，不限于示例中的四个文件。

## 3. 成功响应

HTTP `201 Created`。新增文件和覆盖已有文件均返回 `201`。

示例：假设 APP 暂存区原本为空，本次仅上传内容为 `hello`、大小为 5 字节的 `hello.txt`：

```json
{
  "appid": "demo",
  "files": [
    {
      "path": "hello.txt",
      "size": 5,
      "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    }
  ],
  "total_bytes": 5
}
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `appid` | string | 所属 APP |
| `files` | array | 本次成功上传的文件列表，不是整个 workspace 的文件列表 |
| `files[].path` | string | 最终保存的相对路径，包含可选的 `path` 前缀 |
| `files[].size` | integer | 文件大小，单位字节 |
| `files[].sha256` | string | 文件内容的 SHA256，64 位十六进制摘要 |
| `total_bytes` | integer | 上传后整个 APP 暂存 workspace 的总字节数，不只是本次上传量 |

## 4. 错误响应

### 4.1 受控错误格式

```json
{
  "detail": "Workspace storage is unavailable"
}
```

下列消息是当前实现的原文。Apps 应按 HTTP 状态码分类，保存 `detail` 用于展示和排查，不建议只通过英文字符串判断状态。

### 4.2 认证、APP、配额及存储错误

| HTTP | `detail` 原文 | 含义 |
| --- | --- | --- |
| `401` | `Authentication required` | 缺少有效的 Bearer 认证头 |
| `401` | `Invalid token` | Token 无效，例如过期或签名错误 |
| `401` | `Invalid token scope` | Token 用途不正确，需要 conductor 的 access token |
| `404` | `Register the app before uploading workspace files` | APP 未注册 |
| `413` | `Workspace upload byte limit exceeded` | 上传字节数超限；非法 `Content-Length` 也可能命中此分支 |
| `413` | `Workspace file count limit exceeded` | 文件数量超过存储层允许值，另见 multipart 文件数量限制 |
| `413` | `Workspace storage quota exceeded` | 上传后 APP 总文件大小或总文件数超限 |
| `415` | `Use multipart/form-data with one or more files fields` | 请求不是 multipart，例如提交了 JSON |
| `503` | `Workspace storage is unavailable` | 上传存储操作发生系统错误，例如权限、磁盘或目录操作失败 |

### 4.3 文件及路径校验错误

以下均返回 HTTP `422`：

| `detail` 原文 | 含义 |
| --- | --- |
| `At least one file is required` | 没有上传任何文件 |
| `Use files fields with relative filenames` | 文件字段名错误、字段不是文件，或缺少文件名 |
| `Workspace path must be a string` | `path` 不是文本，例如被作为文件提交 |
| `Workspace path must be a relative POSIX path` | 路径不合法，例如绝对路径、以 `~` 开头、包含反斜杠、冒号或控制字符 |
| `Workspace path traversal and .git paths are forbidden` | 包含 `..`、`.git` 路径段，或路径长度超过限制 |
| `Workspace file path is required` | 提供的是 `.` 等无具体文件名的路径 |
| `Workspace upload paths overlap` | 同一批次存在重复目标路径或父子路径冲突，例如同时上传 `a` 和 `a/b.txt` |
| `Workspace upload conflicts with an existing file or directory` | 与已有文件或目录类型冲突，例如已有文件 `a`，却上传 `a/b.txt` |
| `Workspace symbolic links are forbidden` | 存储根目录或目标路径链上存在符号链接 |
| `Workspace contains a symbolic link or special file` | 现有 workspace 中存在符号链接或特殊文件 |

### 4.4 multipart 解析错误

以下是当前部署框架中已确认的解析错误，均返回 HTTP `400`，响应格式同样为 `{"detail":"..."}`：

| `detail` 原文 | 含义 |
| --- | --- |
| `Missing boundary in multipart.` | `Content-Type` 缺少 boundary |
| `The Content-Disposition header field "name" must be provided.` | 某个 multipart 部分缺少字段名 |
| `Too many files. Maximum number of files is 10000.` | 单次请求的文件部分超过当前配置的 10000 个 |
| `Too many fields. Maximum number of fields is 1.` | 文本字段超过 1 个；统计所有文本字段，不仅是 `path` |
| `Part exceeded maximum size of 1024KB.` | 单个文本字段超过 1 MiB，不是文件内容大小限制 |
| `Invalid multipart data.` | 底层解析器检测到 multipart 格式错误 |

单次文件数超限通常先被解析器拦截，返回 `400`，不会进入存储层的 `413` 分支。上述消息可能受框架版本或配额配置影响，也不保证所有畸形请求都会得到相同错误。

### 4.5 非标准错误

未捕获的服务端异常可能返回 HTTP `500`；代理故障还可能产生其他状态码。此类响应不保证是 JSON，也不保证有 `detail` 字段。

超时、断网等情况可能没有 HTTP 响应，不能据此认定服务端完全没有保存文件。客户端应保留网络错误信息，并在恢复后核对文件。

## 5. 当前测试环境限制

| 限制项 | 当前值 | 配置或说明 |
| --- | --- | --- |
| 单次上传所有文件内容合计 | 100 MiB，即 `104857600` 字节 | `ORCHESTRATOR_WORKSPACE_MAX_UPLOAD_BYTES` |
| HTTP 请求体外层限制 | 101 MiB，即 `105906176` 字节 | 文件内容上限加额外 1 MiB multipart 包装空间 |
| 单 APP 暂存文件总大小 | 1 GiB，即 `1073741824` 字节 | `ORCHESTRATOR_WORKSPACE_MAX_BYTES` |
| 单 APP 暂存文件总数 | 10000 个 | `ORCHESTRATOR_WORKSPACE_MAX_FILES` |
| 单次请求文件部分数 | 10000 个 | 解析器使用同一文件数量配置，超限返回 `400` |
| 单次请求文本字段数 | 最多 1 个 | 用于可选的 `path` |
| 单个文本字段大小 | 1 MiB | 当前解析器默认值，不是文件内容上限 |

## 6. Apps 建议状态枚举

以下状态由 Apps 自行定义，不是服务端响应中现有的 `status` 字段。

| 建议状态 | 判断条件 | 含义 |
| --- | --- | --- |
| `uploading` | 客户端请求进行中 | 上传中 |
| `uploaded` | HTTP `201` | 文件上传成功 |
| `invalid_request` | HTTP `400` | 请求格式、multipart 字段数量等不符合要求 |
| `unauthorized` | HTTP `401` | 认证失败 |
| `app_not_registered` | 本上传接口返回上述 APP 未注册的 `404` | 需要先注册 APP |
| `limit_exceeded` | HTTP `413` | 上传或存储额度超限 |
| `unsupported_media_type` | HTTP `415` | 请求不是 multipart |
| `validation_failed` | HTTP `422` | 文件或路径校验失败 |
| `storage_unavailable` | HTTP `503` | workspace 存储操作失败 |
| `server_error` | HTTP `500` 或其他服务端错误 | 服务端或网关异常 |
| `result_unknown` | 超时、断网等无法确认结果 | 服务端可能已保存部分或全部文件，需要核对 |

建议 Apps 同时保存以下信息作为自身记录，注意这些不是本接口统一返回的字段：

```json
{
  "status": "validation_failed",
  "http_status": 422,
  "msg": "Workspace upload paths overlap"
}
```

## 7. 对接及重试注意事项

- 成功按 HTTP `201` 判断；`detail` 是错误原因，不是固定业务错误码。
- `400`、`415`、`422` 应先修正请求或文件，再重新上传。
- `401` 应先取得有效的 access token；`404` 应确认 APP 已注册及 `appid` 正确。
- `413` 应区分单次上传超限和 APP 总配额超限，不能仅靠原样重试解决。
- 文件按同路径覆盖，但整个批次不是保证全有或全无的事务；写入阶段发生存储异常时，可能已有部分文件被替换。
- 遇到超时、网络错误或存储异常，可以调用 `GET /api/v1/workspace/{appid}` 核对文件路径、大小和 SHA256 后决定重传。
- 上传完成后再创建新构建。后续上传不会改变已经冻结的构建快照，也不会自动更新运行实例。
- 运行时使用上传内容还需要 Compose 声明相应挂载，例如 `.:/workspace`；仅设置 `working_dir: /workspace` 不代表已经挂载文件。

更完整的 workspace 工作流及挂载说明见 `docs/workspace.md`。实现位置为 `main.py` 中的 `upload_workspace`、`require_token`、`workspace_error`，以及 `orchestrator/workspaces.py` 中的 `WorkspaceStore.upload`。
