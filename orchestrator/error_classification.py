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
    if metadata is None:
        metadata = {}
    
    # 明确code/subcode对应的安全模板
    if code == "COMPOSE_CONFIG_ERROR":
        if subcode == "MISSING_ENVIRONMENT_VARIABLES":
            # 白名单元数据：missing_variables列表（变量名）
            missing = metadata.get("missing_variables", [])
            if missing:
                # 只使用变量名（已验证为合法标识符）
                return f"缺少必需的环境变量：{', '.join(missing[:5])}"
            return "缺少必需的环境变量"
        elif subcode == "EXTERNAL_STATEFUL_ENDPOINT":
            # 白名单元数据：endpoint_variable（变量名）
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
            # 保守分类为未知
            return {
                "category": ErrorCategory.UNKNOWN_ERROR,
                "responsible_party": ResponsibleParty.UNKNOWN,
                "retryable": False,
                "user_action_required": True,
                "action_guidance": "查看错误详情判断失败原因，或联系平台排查",
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
    
    # 如果已有分类字段，直接返回（不覆盖已有分类）
    if "category" in error_detail and "responsible_party" in error_detail:
        return error_detail
    
    # 提取现有字段用于分类
    classification = classify_error(
        exception=exception,
        error_code=error_detail.get("code"),
        error_subcode=error_detail.get("subcode"),
        failure_site=error_detail.get("failure_site"),
        exception_type=error_detail.get("exception_type"),
        summary=error_detail.get("summary"),
    )
    
    # 合并分类字段（不覆盖已有字段）
    result = {**error_detail}
    for key, value in classification.items():
        if key not in result:
            # 将枚举值转换为字符串（.value）
            if isinstance(value, (ErrorCategory, ResponsibleParty)):
                result[key] = value.value
            else:
                result[key] = value
    
    # 确保summary字段存在且安全（从模板生成）
    # 不信任任何传入的summary/remediation/error字段
    if "summary" not in result or not result["summary"]:
        # 如果有明确code/subcode，从模板生成
        if result.get("code") and result.get("subcode"):
            result["summary"] = get_safe_summary_from_template(
                result["code"],
                result["subcode"],
                result  # 传入整个result作为metadata（包含白名单字段）
            )
        else:
            # 未知错误，使用action_guidance或通用模板
            result["summary"] = result.get("action_guidance", "构建失败，请联系平台排查")
    else:
        # 已有summary，但不能信任，从模板重新生成
        if result.get("code") and result.get("subcode"):
            result["summary"] = get_safe_summary_from_template(
                result["code"],
                result["subcode"],
                result
            )
        else:
            # 无code/subcode，覆盖为安全模板
            result["summary"] = result.get("action_guidance", "构建失败，请联系平台排查")
    
    # 确保code和subcode字段存在（即使为None）
    if "code" not in result:
        result["code"] = None
    if "subcode" not in result:
        result["subcode"] = None
    
    # 确保recognized字段存在
    if "recognized" not in result:
        result["recognized"] = False
    
    # 清理不安全的字段：只保留白名单字段
    # 安全白名单字段：code, subcode, category, responsible_party, retryable, user_action_required,
    #                 action_guidance, summary, recognized, failure_site, build_id,
    #                 endpoint_variable, missing_variables, config_path（合法元数据）
    safe_fields = {
        "code", "subcode", "category", "responsible_party", "retryable",
        "user_action_required", "action_guidance", "summary", "recognized",
        "failure_site", "build_id", "stage", "source",
        # 白名单元数据
        "endpoint_variable", "missing_variables", "config_path",
        "exception_type",  # 保留但不信任
    }
    
    # 移除不在白名单的字段（如remediation, error, detail等可能含secret）
    result = {k: v for k, v in result.items() if k in safe_fields}
    
    return result
