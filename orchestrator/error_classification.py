"""统一构建错误分类契约

为APPS提供稳定的机器字段判断错误类别、责任方、是否需要用户操作、是否可重试等。
不依赖英文summary、exception_type或failure_site做分类。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    """错误类别枚举"""
    CONFIGURATION_ERROR = "configuration_error"  # 配置错误（需修改应用配置）
    INFRASTRUCTURE_ERROR = "infrastructure_error"  # 基础设施错误（Jenkins/Docker/网络不可达）
    APPLICATION_BUILD_ERROR = "application_build_error"  # 应用构建失败（Jenkins构建失败）
    APPLICATION_RUNTIME_ERROR = "application_runtime_error"  # 应用运行失败（部署后服务启动失败）
    TIMEOUT_ERROR = "timeout_error"  # 超时错误
    UNKNOWN_ERROR = "unknown_error"  # 未知错误


class ResponsibleParty(str, Enum):
    """责任方枚举"""
    APPLICATION_DEVELOPER = "application_developer"  # 应用开发者（需修改代码/配置）
    PLATFORM = "platform"  # 平台（需平台处理）
    UNKNOWN = "unknown"  # 未知（需排查）


def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """验证并清理metadata，保留完整结构但验证类型和格式，不能夹带secret"""
    if not isinstance(metadata, dict):
        return {}
    
    safe = {}
    
    # endpoint_variable: 必须是合法标识符
    if "endpoint_variable" in metadata:
        var = metadata["endpoint_variable"]
        if isinstance(var, str) and var and var.replace("_", "").isalnum():
            safe["endpoint_variable"] = var
    
    # missing_variables: 保留完整结构 name/reason/operator/locations
    # 契约: name(str), reason(missing|empty), operator(允许列表), locations([{service, environment_key, config_path}])
    # 白名单：只保留这4个顶层字段，删除任何未知字段（如value等）
    if "missing_variables" in metadata:
        missing = metadata["missing_variables"]
        if isinstance(missing, list):
            safe_vars = []
            # 不截断：用户目标是全部
            for item in missing:
                if isinstance(item, dict):
                    safe_item = {}
                    # name: 必须是合法标识符
                    name = item.get("name", "")
                    if isinstance(name, str) and name and name.replace("_", "").isalnum():
                        safe_item["name"] = name
                    else:
                        continue  # 无效name，跳过整个item
                    
                    # reason: 允许值 missing|empty
                    reason = item.get("reason", "missing")
                    if reason in ("missing", "empty"):
                        safe_item["reason"] = reason
                    
                    # operator: 保留允许的值（None或操作符字符串）
                    if "operator" in item:
                        operator = item["operator"]
                        if operator is None:
                            safe_item["operator"] = None
                        elif isinstance(operator, str) and len(operator) < 10:
                            safe_item["operator"] = operator
                    
                    # locations: 验证数组，只保留合法字段（service, environment_key, config_path）
                    if "locations" in item and isinstance(item["locations"], list):
                        safe_locations = []
                        # 不截断：用户目标是全部位置
                        for loc in item["locations"]:
                            if isinstance(loc, dict):
                                # 只保留白名单字段，删除所有未知字段
                                safe_loc = {}
                                # service: 合法服务名
                                if "service" in loc and isinstance(loc["service"], str) and len(loc["service"]) < 100:
                                    safe_loc["service"] = loc["service"]
                                # environment_key: 合法标识符
                                if "environment_key" in loc and isinstance(loc["environment_key"], str):
                                    if loc["environment_key"].replace("_", "").isalnum():
                                        safe_loc["environment_key"] = loc["environment_key"]
                                # config_path: 路径格式
                                if "config_path" in loc and isinstance(loc["config_path"], str):
                                    if len(loc["config_path"]) < 200:
                                        safe_loc["config_path"] = loc["config_path"]
                                # 至少有一个字段才保留
                                if safe_loc:
                                    safe_locations.append(safe_loc)
                        if safe_locations:
                            safe_item["locations"] = safe_locations
                    
                    # 只保留白名单的4个字段：name, reason, operator, locations
                    # 删除任何其他字段如value, hint等
                    safe_vars.append(safe_item)
                elif isinstance(item, str) and item and item.replace("_", "").isalnum():
                    # 兼容旧格式：纯字符串变量名
                    safe_vars.append({"name": item, "reason": "missing"})
            
            if safe_vars:
                safe["missing_variables"] = safe_vars
    
    # config_path: 只保留路径格式（不含值）
    if "config_path" in metadata:
        path = metadata["config_path"]
        if isinstance(path, str) and len(path) < 200:
            safe["config_path"] = path
    
    # check_scope, failure_site, stage, source: 保留已有上下文字段
    for field in ("check_scope", "failure_site", "stage", "source", "service"):
        if field in metadata and isinstance(metadata[field], str) and len(metadata[field]) < 100:
            safe[field] = metadata[field]
    
    # exception_type: 保留类型名（但不信任）
    if "exception_type" in metadata and isinstance(metadata["exception_type"], str):
        safe["exception_type"] = metadata["exception_type"]
    
    return safe


def get_safe_summary_from_template(code: str | None, subcode: str | None, metadata: dict[str, Any] | None = None) -> str:
    """
    从code/subcode生成安全的中文摘要（仅使用明确白名单元数据）
    
    Args:
        code: 错误代码
        subcode: 错误子代码
        metadata: 明确白名单元数据（变量名、compose位置等）
    
    Returns:
        安全的中文错误摘要
    """
    metadata = _sanitize_metadata(metadata or {})
    
    # 明确code/subcode对应的安全模板
    if code == "COMPOSE_CONFIG_ERROR":
        if subcode == "MISSING_ENVIRONMENT_VARIABLES":
            missing_vars = metadata.get("missing_variables", [])
            if missing_vars:
                # 从完整结构中提取name字段
                var_names = []
                for item in missing_vars[:5]:
                    if isinstance(item, dict):
                        var_names.append(item.get("name", ""))
                    elif isinstance(item, str):
                        var_names.append(item)
                var_names = [v for v in var_names if v]
                if var_names:
                    return f"缺少必需的环境变量：{', '.join(var_names)}"
            return "缺少必需的环境变量"
        elif subcode == "EXTERNAL_STATEFUL_ENDPOINT":
            var = metadata.get("endpoint_variable", "")
            if var:
                return f"环境变量 {var} 指向外部有状态端点，无法保证构建隔离"
            return "构建配置包含外部有状态端点"
        elif subcode == "INVALID_STATEFUL_ENDPOINT":
            var = metadata.get("endpoint_variable", "")
            if var:
                return f"环境变量 {var} 配置的有状态端点格式无效"
            return "有状态端点配置格式无效"
        elif subcode == "UNSUPPORTED_GIT_BIND_MOUNT":
            return "不支持将Git仓库目录绑定到容器（构建隔离要求）"
    
    # 未知或无模板，返回通用中文摘要
    return "构建失败，请联系平台排查"


def get_safe_remediation_from_template(code: str | None, subcode: str | None, metadata: dict[str, Any] | None = None) -> str:
    """从code/subcode生成安全的remediation"""
    metadata = _sanitize_metadata(metadata or {})
    
    if code == "COMPOSE_CONFIG_ERROR":
        if subcode == "MISSING_ENVIRONMENT_VARIABLES":
            missing_vars = metadata.get("missing_variables", [])
            if missing_vars:
                # 从完整结构中提取name字段
                var_names = []
                for item in missing_vars[:5]:
                    if isinstance(item, dict):
                        var_names.append(item.get("name", ""))
                    elif isinstance(item, str):
                        var_names.append(item)
                var_names = [v for v in var_names if v]
                if var_names:
                    return f"在应用配置或构建参数中补充：{', '.join(var_names)}"
            return "补充缺失的环境变量"
        elif subcode == "EXTERNAL_STATEFUL_ENDPOINT":
            var = metadata.get("endpoint_variable", "")
            if var:
                service_hint = var.replace("_URL", "").replace("_URI", "").replace("_HOST", "").lower()
                return f"在同一构建的 services 中定义 {service_hint} 服务，使用服务名连接。例如：{var}=postgres://db:5432/mydb"
            return "在同一构建的 services 中定义对应服务，使用服务名连接"
        elif subcode == "INVALID_STATEFUL_ENDPOINT":
            return "检查数据库连接字符串格式，或在同一构建中定义服务使用服务名连接"
        elif subcode == "UNSUPPORTED_GIT_BIND_MOUNT":
            return "移除Compose中的Git仓库目录绑定，在Dockerfile中使用COPY指令或使用命名卷"
    
    return "联系平台排查具体原因"


def classify_error(
    *,
    exception: Exception | None = None,
    error_code: str | None = None,
    error_subcode: str | None = None,
    failure_site: str | None = None,
    exception_type: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """根据异常类型和上下文分类错误
    
    Args:
        exception: 原始异常对象（优先级最高）
        error_code: 错误代码
        error_subcode: 错误子代码
        failure_site: 失败阶段
        exception_type: 异常类型名称
        summary: 错误摘要
        
    Returns:
        包含category, responsible_party, retryable, user_action_required的字典
    """
    from orchestrator.services import BuildInputError
    from orchestrator.docker_services import DockerServiceError
    from orchestrator.jenkins import JenkinsError
    from orchestrator.workspaces import ComposeConfigError
    
    # 优先使用已有的code/subcode（明确分类）
    if error_code == "COMPOSE_CONFIG_ERROR":
        if error_subcode == "MISSING_ENVIRONMENT_VARIABLES":
            return {
                "category": ErrorCategory.CONFIGURATION_ERROR,
                "responsible_party": ResponsibleParty.APPLICATION_DEVELOPER,
                "retryable": False,
                "user_action_required": True,
                "action_guidance": "补充缺失的环境变量后重新创建构建",
            }
        elif error_subcode == "UNSUPPORTED_GIT_BIND_MOUNT":
            return {
                "category": ErrorCategory.CONFIGURATION_ERROR,
                "responsible_party": ResponsibleParty.APPLICATION_DEVELOPER,
                "retryable": False,
                "user_action_required": True,
                "action_guidance": "修改Compose配置，使用Dockerfile COPY或命名卷",
            }
        elif error_subcode == "EXTERNAL_STATEFUL_ENDPOINT":
            return {
                "category": ErrorCategory.CONFIGURATION_ERROR,
                "responsible_party": ResponsibleParty.APPLICATION_DEVELOPER,
                "retryable": False,
                "user_action_required": True,
                "action_guidance": "在同一构建中定义数据库服务，使用服务名连接",
            }
        elif error_subcode == "INVALID_STATEFUL_ENDPOINT":
            return {
                "category": ErrorCategory.CONFIGURATION_ERROR,
                "responsible_party": ResponsibleParty.APPLICATION_DEVELOPER,
                "retryable": False,
                "user_action_required": True,
                "action_guidance": "检查数据库连接字符串格式",
            }
    
    # 使用异常类型判断
    if exception is not None:
        exception_type = type(exception).__name__
        
        if isinstance(exception, ComposeConfigError):
            return {
                "category": ErrorCategory.CONFIGURATION_ERROR,
                "responsible_party": ResponsibleParty.APPLICATION_DEVELOPER,
                "retryable": False,
                "user_action_required": True,
                "action_guidance": "修改Compose配置后重新创建构建",
            }
        
        if isinstance(exception, BuildInputError):
            # BuildInputError without explicit code/subcode → unknown_error/unknown
            # Only BuildInputError with explicit code/subcode (from typed source) is configuration_error
            # 这里是fallback，保守分类为unknown
            return {
                "category": ErrorCategory.UNKNOWN_ERROR,
                "responsible_party": ResponsibleParty.UNKNOWN,
                "retryable": False,
                "user_action_required": False,
                "action_guidance": "构建输入验证失败，请联系平台排查具体原因",
            }
        
        if isinstance(exception, JenkinsError):
            # Jenkins错误可能是基础设施或应用构建失败
            # 仅failure_site不足以证明责任方（可能是节点离线/推镜像/平台凭据等）
            # 保守分类为未知，需要查看日志判断
            return {
                "category": ErrorCategory.UNKNOWN_ERROR,
                "responsible_party": ResponsibleParty.UNKNOWN,
                "retryable": False,
                "user_action_required": True,
                "action_guidance": "查看构建日志判断失败原因，或联系平台排查",
            }
        
        if isinstance(exception, DockerServiceError):
            # DockerServiceError可能是基础设施或应用运行时错误
            # 仅exception类型和failure_site不足以证明责任方
            # （可能是Docker API不可达/平台资源不足）
            # 保守分类为未知，不要求用户操作
            return {
                "category": ErrorCategory.UNKNOWN_ERROR,
                "responsible_party": ResponsibleParty.UNKNOWN,
                "retryable": False,
                "user_action_required": False,
                "action_guidance": "构建失败，请联系平台排查",
            }
    
    # 使用failure_site判断（次优）
    if failure_site == "poll_timeout":
        return {
            "category": ErrorCategory.TIMEOUT_ERROR,
            "responsible_party": ResponsibleParty.UNKNOWN,
            "retryable": False,
            "user_action_required": True,
            "action_guidance": "检查是否有正在运行的构建，避免重复创建",
        }
    
    # 未知错误（保守分类）
    return {
        "category": ErrorCategory.UNKNOWN_ERROR,
        "responsible_party": ResponsibleParty.UNKNOWN,
        "retryable": False,
        "user_action_required": True,
        "action_guidance": "联系平台排查具体错误原因",
    }


def enrich_error_detail(
    error_detail: dict[str, Any] | None,
    exception: Exception | None = None,
) -> dict[str, Any]:
    """enrichment existing error_detail with classification fields
    
    Args:
        error_detail: 现有的error_detail字典
        exception: 原始异常对象
        
    Returns:
        增强后的error_detail，包含category, responsible_party, retryable等字段和安全summary
    """
    if error_detail is None:
        error_detail = {}
    
    # 不能提前返回：即使已有分类字段，也必须安全规范化summary/remediation
    # 已有的category/responsible_party可能来自不可信源，summary也可能含secret
    
    # 提取现有字段用于分类
    classification = classify_error(
        exception=exception,
        error_code=error_detail.get("code"),
        error_subcode=error_detail.get("subcode"),
        failure_site=error_detail.get("failure_site"),
        exception_type=error_detail.get("exception_type"),
        summary=error_detail.get("summary"),
    )
    
    # 强制覆盖所有分类字段（不信任来路不明的已有值）
    result = {**error_detail}
    for key, value in classification.items():
        # 将枚举值转换为字符串（.value）
        if isinstance(value, (ErrorCategory, ResponsibleParty)):
            result[key] = value.value
        else:
            result[key] = value
    
    # 确保summary和remediation字段存在且安全（从模板生成）
    # 不信任任何传入的summary/remediation/error字段
    if result.get("code") and result.get("subcode"):
        # 从模板生成summary和remediation
        result["summary"] = get_safe_summary_from_template(
            result["code"],
            result["subcode"],
            result  # 传入整个result作为metadata（包含白名单字段）
        )
        result["remediation"] = get_safe_remediation_from_template(
            result["code"],
            result["subcode"],
            result
        )
    else:
        # 无code/subcode：仅允许精确匹配的受控平台常量消息表
        # 任何任意异常消息不能因短/中文/不含关键词而原样公开
        # 不能使用任意已有action_guidance作模板（可能含注入）
        SAFE_PLATFORM_MESSAGES = {
            "未进行路由静态检查, 无法交付构建任务",
            "Route check service is not configured",
        }
        if exception:
            exc_msg = str(exception)
            # 只保留精确匹配的受控平台常量
            if exc_msg in SAFE_PLATFORM_MESSAGES:
                result["summary"] = exc_msg
            else:
                # 其他任意消息，使用固定中文模板（不信任action_guidance）
                result["summary"] = "构建失败，请联系平台排查"
        else:
            result["summary"] = "构建失败，请联系平台排查"
        result["remediation"] = "联系平台排查具体原因"
    
    # 确保code和subcode字段存在且不为null
    # code/subcode缺失时必须明确设置，不允许null
    if not result.get("code"):
        result["code"] = "UNKNOWN_CODE"
    if not result.get("subcode"):
        result["subcode"] = "UNKNOWN_SUBCODE"
    
    # 确保recognized字段存在且正确
    # 只有明确的code/subcode才算recognized
    if result.get("code") not in (None, "UNKNOWN_CODE") and result.get("subcode") not in (None, "UNKNOWN_SUBCODE"):
        result["recognized"] = True
    else:
        result["recognized"] = False
    
    # 生成summary前需要先过滤字段，但summary生成需要missing_variables
    # 所以在生成summary后再过滤
    
    # 深层清理不安全的字段：只保留白名单字段
    # 安全白名单字段：code, subcode, category, responsible_party, retryable, user_action_required,
    #                 action_guidance, summary, remediation（从模板生成）, recognized, failure_site, build_id,
    #                 endpoint_variable, missing_variables（需深层验证）, config_path, check_scope, service（合法元数据）
    safe_fields = {
        "code", "subcode", "category", "responsible_party", "retryable",
        "user_action_required", "action_guidance", "summary", "remediation", "recognized",
        "failure_site", "build_id", "stage", "source",
        # 白名单元数据（已通过_sanitize_metadata验证或为合法技术字段）
        "endpoint_variable", "missing_variables", "config_path", "check_scope", "service",
        "exception_type",  # 保留但不信任
    }
    
    # 移除不在白名单的字段（如error, detail等可能含secret）
    result = {k: v for k, v in result.items() if k in safe_fields}
    
    # 对missing_variables做深层白名单过滤（删除value等未知字段）
    if "missing_variables" in result:
        result = dict(result)  # 确保是可变dict
        result["missing_variables"] = _sanitize_metadata({"missing_variables": result["missing_variables"]}).get("missing_variables", [])
    
    return result
