# APPS 平台原始失败日志访问指南

## 概述

从 2026-11-26 更新开始，docker-jenkins-orchestrator 在构建失败时会尽可能返回完整的原始失败日志，以便 APPS 开发者能够快速定位真实的构建问题。

## 变更说明

### 增强的日志捕获

- **增大日志容量**：`raw_log_excerpt` 默认最大容量从 8000 字符增加到 32000 字符
- **增强上下文**：失败位置前后的上下文行数分别从 12/20 增加到 20/40 行
- **容错处理**：即使日志解析失败，也会尝试获取 Jenkins console 尾部的原始内容
- **新错误类型识别**：增加了对 "build context does not exist" 和 "workspace does not exist" 错误的识别

### APPS 应读取的字段优先级

当构建失败时（`status == "failed"`），APPS 平台应按以下优先级读取失败信息：

#### 1. 原始日志摘录（推荐）

```python
def get_failure_log(build: dict) -> str | None:
    """获取完整的原始失败日志（推荐用于问题诊断）"""
    return build.get("raw_log_excerpt")
```

**字段说明**：
- 字段名：`raw_log_excerpt`
- 类型：`string | null`
- 内容：脱敏后的 Jenkins console 日志摘录，包含失败位置前后的完整上下文
- 大小：最大 32000 字符
- 格式：带行号标记的文本，失败行以 `>>` 标记

**示例**：
```
  L71: [Pipeline] { (Validate Compose)
  L72: [2026-11-26T08:00:01.123Z] Validating Compose structure...
>> L73: [2026-11-26T08:00:01.456Z] ERROR: service ai-worker build context does not exist: ./backend
  L74: [Pipeline] }
  L75: [Pipeline] // stage
  L76: ERROR: script returned exit code 1
```

#### 2. 结构化错误详情

```python
def get_error_detail(build: dict) -> dict:
    """获取结构化的错误分类和上下文"""
    return build.get("error_detail", {})
```

**关键子字段**：
- `stage`：失败阶段（如 "Validate Compose", "Build And Push"）
- `cause`：错误类型（如 "文件或路径缺失", "连接失败", "DNS 解析失败"）
- `source_file`：出错的源文件位置（如 "Dockerfile:4"）
- `location`：控制台行号（如 "console L73"）
- `reason`：错误原因的原始文本
- `exit_code`：退出码
- `jenkins_build_number`：Jenkins 构建号
- `recognized`：是否为已识别的错误类型
- `summary`：结构化摘要（中文，包含阶段/位置/原因等）

#### 3. 失败日志袋（备用）

```python
def get_failure_logs(build: dict) -> dict | None:
    """获取失败日志袋（包含来源和摘录）"""
    return build.get("failure_logs")
```

**字段说明**：
- `source`：日志来源（如 "jenkins_console", "container_logs"）
- `raw_log_excerpt`：日志摘录（与顶层字段内容相同）
- `unavailable`：布尔值，表示日志是否不可用

#### 4. 错误摘要（向后兼容）

```python
def get_error_message(build: dict) -> str:
    """获取简短的错误摘要（向后兼容）"""
    return build.get("error", "构建失败，详情不可用")
```

## 使用建议

### 推荐做法

1. **优先展示原始日志**：在失败详情页面中优先展示 `raw_log_excerpt`，因为它包含最完整的诊断信息
2. **结构化字段辅助**：使用 `error_detail` 字段提取关键信息（阶段、类型、文件位置）用于列表展示和快速筛选
3. **提供 Jenkins 链接**：当 `error_detail.jenkins_build_number` 存在时，提供到 Jenkins console 的直接链接
4. **向后兼容**：为历史构建（无 `raw_log_excerpt`）保留 `error` 字段的展示

### 示例代码

```python
def render_build_failure(build: dict) -> dict:
    """渲染构建失败信息"""
    error_detail = build.get("error_detail", {})
    raw_log = build.get("raw_log_excerpt")
    
    result = {
        "title": "构建失败",
        "stage": error_detail.get("stage", "未知阶段"),
        "cause": error_detail.get("cause", ""),
        "jenkins_build": error_detail.get("jenkins_build_number"),
        "has_raw_log": bool(raw_log),
    }
    
    # 优先使用原始日志
    if raw_log:
        result["detail_text"] = raw_log
        result["detail_type"] = "raw_log"
    else:
        # 降级到摘要
        result["detail_text"] = build.get("error", "构建失败，详情不可用")
        result["detail_type"] = "summary"
    
    # 添加诊断提示
    if error_detail.get("cause") == "文件或路径缺失":
        result["hint"] = "请检查 Dockerfile 路径、build context 配置和依赖文件是否存在"
    elif error_detail.get("cause") == "连接失败":
        result["hint"] = "请检查网络连接和镜像仓库可访问性"
    
    return result
```

## 配置调整（可选）

如果需要调整日志大小限制，可以通过环境变量配置：

```bash
# 增大或减小日志摘录大小（字符数）
ORCHESTRATOR_FAILURE_LOG_EXCERPT_MAX_CHARS=50000

# 增大 Jenkins console 读取窗口（字节数）
ORCHESTRATOR_JENKINS_CONSOLE_WINDOW_BYTES=524288
```

**注意**：过大的配置可能影响 API 响应性能和数据库存储。推荐保持默认值（32000 字符）。

## 常见问题

### Q: `raw_log_excerpt` 为什么有时是 null？

A: 以下情况可能导致 `raw_log_excerpt` 为空：
1. 构建失败发生在 Jenkins 外部（如 precheck、validating 阶段）
2. Jenkins API 请求失败或超时
3. Jenkins 构建还未完成或日志未生成
4. 历史构建（更新前创建的）

这些情况下应降级使用 `error` 字段。

### Q: 日志是否经过脱敏？

A: 是的。`raw_log_excerpt` 已经过以下脱敏处理：
- 应用环境变量值
- Compose environment/build args
- URL 中的用户名和密码
- 查询参数
- Bearer tokens、Basic auth 等认证信息
- 私钥内容

因此可以安全地在前端展示，无需额外脱敏。

### Q: 如何判断日志是否被截断？

A: 检查日志末尾是否有省略号 `…`。如果有，表示原始日志超过了大小限制。
建议在 UI 中提示用户查看完整的 Jenkins console。

### Q: `error_detail.recognized` 的含义？

A: 表示错误是否被自动分类识别：
- `true`：已识别的常见错误类型，`cause` 字段有值
- `false`：未识别的错误，可能需要人工介入排查

## 迁移时间线

- **2026-11-26**：新版本部署
- **生效范围**：新版本部署后创建的构建
- **历史构建**：不会回填新字段，保持原有行为
- **向后兼容**：`error` 和 `failure_site` 字段保持不变
- **APPS 适配**：建议在新版本上线后一周内完成前端适配
