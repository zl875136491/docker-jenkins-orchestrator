"""统一构建错误分类契约

为APPS提供稳定的机器字段判断错误类别、责任方、是否需要用户操作、是否可重试等。
不依赖英文summary、exception_type或failure_site做分类。
"""

from __future__ import annotations

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
            # BuildInputError可能是配置错误，需要进一步判断
            msg = str(exception).lower()
            
            # 外部stateful endpoint
            if "external stateful endpoint" in msg and "cannot be proven build-private" in msg:
                return {
                    "category": ErrorCategory.CONFIGURATION_ERROR,
                    "responsible_party": ResponsibleParty.APPLICATION_DEVELOPER,
                    "retryable": False,
                    "user_action_required": True,
                    "action_guidance": "在同一构建中定义数据库服务，使用服务名连接，不使用外部URI",
                }
            
            # 路由检查未通过
            if "未进行路由静态检查" in msg or "build gate" in msg.lower():
                return {
                    "category": ErrorCategory.CONFIGURATION_ERROR,
                    "responsible_party": ResponsibleParty.APPLICATION_DEVELOPER,
                    "retryable": False,
                    "user_action_required": True,
                    "action_guidance": "先通过静态路由检查后再创建构建",
                }
            
            # 其他BuildInputError默认为配置错误
            return {
                "category": ErrorCategory.CONFIGURATION_ERROR,
                "responsible_party": ResponsibleParty.APPLICATION_DEVELOPER,
                "retryable": False,
                "user_action_required": True,
                "action_guidance": "检查并修复配置错误后重试",
            }
        
        if isinstance(exception, JenkinsError):
            # Jenkins错误可能是基础设施或应用构建失败
            # 需要根据failure_site和具体内容判断
            if failure_site == "triggering":
                return {
                    "category": ErrorCategory.INFRASTRUCTURE_ERROR,
                    "responsible_party": ResponsibleParty.PLATFORM,
                    "retryable": True,
                    "user_action_required": False,
                    "action_guidance": "稍后重试，如持续失败请联系平台排查",
                }
            elif failure_site == "jenkins":
                # Jenkins构建失败，大多数是应用代码问题
                return {
                    "category": ErrorCategory.APPLICATION_BUILD_ERROR,
                    "responsible_party": ResponsibleParty.APPLICATION_DEVELOPER,
                    "retryable": False,
                    "user_action_required": True,
                    "action_guidance": "查看构建日志，修复代码或构建配置后重试",
                }
            else:
                return {
                    "category": ErrorCategory.INFRASTRUCTURE_ERROR,
                    "responsible_party": ResponsibleParty.PLATFORM,
                    "retryable": True,
                    "user_action_required": False,
                    "action_guidance": "稍后重试，如持续失败请联系平台排查",
                }
        
        if isinstance(exception, DockerServiceError):
            msg = str(exception).lower()
            
            # 外部stateful endpoint（特定DockerServiceError）
            if "external stateful endpoint" in msg:
                return {
                    "category": ErrorCategory.CONFIGURATION_ERROR,
                    "responsible_party": ResponsibleParty.APPLICATION_DEVELOPER,
                    "retryable": False,
                    "user_action_required": True,
                    "action_guidance": "在同一构建中定义数据库服务，使用服务名连接",
                }
            
            # 其他DockerServiceError可能是基础设施或应用运行时错误
            if failure_site == "deploy":
                return {
                    "category": ErrorCategory.APPLICATION_RUNTIME_ERROR,
                    "responsible_party": ResponsibleParty.APPLICATION_DEVELOPER,
                    "retryable": False,
                    "user_action_required": True,
                    "action_guidance": "查看服务日志，修复应用运行时问题",
                }
            else:
                return {
                    "category": ErrorCategory.INFRASTRUCTURE_ERROR,
                    "responsible_party": ResponsibleParty.PLATFORM,
                    "retryable": True,
                    "user_action_required": False,
                    "action_guidance": "稍后重试，如持续失败请联系平台排查",
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
        增强后的error_detail，包含category, responsible_party, retryable等字段
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
    
    return result
