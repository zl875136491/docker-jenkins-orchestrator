"""Tests for unified error classification contract.

Verifies that error classification correctly categorizes errors and provides
stable machine-readable fields for APPS.
"""

import pytest

from orchestrator.error_classification import (
    ErrorCategory,
    ResponsibleParty,
    classify_error,
    enrich_error_detail,
)
from orchestrator.services import BuildInputError
from orchestrator.docker_services import DockerServiceError
from orchestrator.jenkins import JenkinsError
from orchestrator.workspaces import ComposeConfigError


def test_classify_missing_environment_variables():
    """Missing environment variables are configuration errors requiring user action."""
    result = classify_error(
        error_code="COMPOSE_CONFIG_ERROR",
        error_subcode="MISSING_ENVIRONMENT_VARIABLES",
    )
    
    assert result["category"] == ErrorCategory.CONFIGURATION_ERROR
    assert result["responsible_party"] == ResponsibleParty.APPLICATION_DEVELOPER
    assert result["retryable"] is False
    assert result["user_action_required"] is True
    assert "补充缺失的环境变量" in result["action_guidance"]


def test_classify_unsupported_git_bind_mount():
    """Unsupported Git bind mounts are configuration errors."""
    result = classify_error(
        error_code="COMPOSE_CONFIG_ERROR",
        error_subcode="UNSUPPORTED_GIT_BIND_MOUNT",
    )
    
    assert result["category"] == ErrorCategory.CONFIGURATION_ERROR
    assert result["responsible_party"] == ResponsibleParty.APPLICATION_DEVELOPER
    assert result["retryable"] is False
    assert result["user_action_required"] is True
    assert "COPY" in result["action_guidance"] or "命名卷" in result["action_guidance"]


def test_classify_external_stateful_endpoint_via_exception():
    """External stateful endpoint error from DockerServiceError is configuration error."""
    exc = DockerServiceError("External stateful endpoint MONGO_URI cannot be proven build-private; use a service in this build")
    
    result = classify_error(exception=exc)
    
    assert result["category"] == ErrorCategory.CONFIGURATION_ERROR
    assert result["responsible_party"] == ResponsibleParty.APPLICATION_DEVELOPER
    assert result["retryable"] is False
    assert result["user_action_required"] is True
    assert "数据库服务" in result["action_guidance"]


def test_classify_external_stateful_endpoint_via_buildinputerror():
    """External stateful endpoint error can also be BuildInputError."""
    exc = BuildInputError("External stateful endpoint REDIS_URL cannot be proven build-private; use a service in this build")
    
    result = classify_error(exception=exc)
    
    assert result["category"] == ErrorCategory.CONFIGURATION_ERROR
    assert result["responsible_party"] == ResponsibleParty.APPLICATION_DEVELOPER
    assert result["retryable"] is False
    assert result["user_action_required"] is True


def test_classify_compose_config_error():
    """ComposeConfigError is configuration error."""
    exc = ComposeConfigError(
        "Invalid Compose configuration",
        code="COMPOSE_CONFIG_ERROR",
        subcode="INVALID_FORMAT",
    )
    
    result = classify_error(exception=exc)
    
    assert result["category"] == ErrorCategory.CONFIGURATION_ERROR
    assert result["responsible_party"] == ResponsibleParty.APPLICATION_DEVELOPER
    assert result["retryable"] is False
    assert result["user_action_required"] is True


def test_classify_build_gate_error():
    """Build gate error (未进行路由静态检查) is configuration error."""
    exc = BuildInputError("未进行路由静态检查, 无法交付构建任务")
    
    result = classify_error(exception=exc)
    
    assert result["category"] == ErrorCategory.CONFIGURATION_ERROR
    assert result["responsible_party"] == ResponsibleParty.APPLICATION_DEVELOPER
    assert result["retryable"] is False
    assert result["user_action_required"] is True
    assert "静态路由检查" in result["action_guidance"]


def test_classify_jenkins_triggering_error():
    """Jenkins triggering error is infrastructure error (platform responsibility)."""
    exc = JenkinsError("Failed to trigger Jenkins build")
    
    result = classify_error(exception=exc, failure_site="triggering")
    
    assert result["category"] == ErrorCategory.INFRASTRUCTURE_ERROR
    assert result["responsible_party"] == ResponsibleParty.PLATFORM
    assert result["retryable"] is True
    assert result["user_action_required"] is False


def test_classify_jenkins_build_failure():
    """Jenkins build failure (failure_site=jenkins) is application build error."""
    exc = JenkinsError("Jenkins build #123 failed")
    
    result = classify_error(exception=exc, failure_site="jenkins")
    
    assert result["category"] == ErrorCategory.APPLICATION_BUILD_ERROR
    assert result["responsible_party"] == ResponsibleParty.APPLICATION_DEVELOPER
    assert result["retryable"] is False
    assert result["user_action_required"] is True
    assert "构建日志" in result["action_guidance"]


def test_classify_docker_deploy_error():
    """DockerServiceError during deploy is application runtime error."""
    exc = DockerServiceError("Service failed to start")
    
    result = classify_error(exception=exc, failure_site="deploy")
    
    assert result["category"] == ErrorCategory.APPLICATION_RUNTIME_ERROR
    assert result["responsible_party"] == ResponsibleParty.APPLICATION_DEVELOPER
    assert result["retryable"] is False
    assert result["user_action_required"] is True


def test_classify_poll_timeout():
    """Poll timeout is timeout error with unknown responsibility."""
    result = classify_error(failure_site="poll_timeout")
    
    assert result["category"] == ErrorCategory.TIMEOUT_ERROR
    assert result["responsible_party"] == ResponsibleParty.UNKNOWN
    assert result["retryable"] is False
    assert result["user_action_required"] is True
    assert "正在运行的构建" in result["action_guidance"]


def test_classify_unknown_error():
    """Unknown errors are classified conservatively."""
    exc = Exception("Unknown error")
    
    result = classify_error(exception=exc)
    
    assert result["category"] == ErrorCategory.UNKNOWN_ERROR
    assert result["responsible_party"] == ResponsibleParty.UNKNOWN
    assert result["retryable"] is False
    assert result["user_action_required"] is True
    assert "联系平台排查" in result["action_guidance"]


def test_enrich_error_detail_adds_classification():
    """enrich_error_detail adds classification fields to existing error_detail."""
    error_detail = {
        "code": "COMPOSE_CONFIG_ERROR",
        "subcode": "MISSING_ENVIRONMENT_VARIABLES",
        "summary": "Missing variables",
    }
    
    enriched = enrich_error_detail(error_detail)
    
    assert enriched["code"] == "COMPOSE_CONFIG_ERROR"
    assert enriched["subcode"] == "MISSING_ENVIRONMENT_VARIABLES"
    assert enriched["summary"] == "Missing variables"
    assert enriched["category"] == ErrorCategory.CONFIGURATION_ERROR
    assert enriched["responsible_party"] == ResponsibleParty.APPLICATION_DEVELOPER
    assert enriched["retryable"] is False
    assert enriched["user_action_required"] is True


def test_enrich_error_detail_preserves_existing_classification():
    """enrich_error_detail does not overwrite existing classification fields."""
    error_detail = {
        "code": "CUSTOM_ERROR",
        "category": "custom_category",
        "responsible_party": "custom_party",
        "retryable": True,
    }
    
    enriched = enrich_error_detail(error_detail)
    
    # Should preserve existing fields
    assert enriched["category"] == "custom_category"
    assert enriched["responsible_party"] == "custom_party"
    assert enriched["retryable"] is True


def test_enrich_error_detail_handles_none():
    """enrich_error_detail handles None error_detail."""
    enriched = enrich_error_detail(None)
    
    assert isinstance(enriched, dict)
    assert enriched["category"] == ErrorCategory.UNKNOWN_ERROR
    assert enriched["responsible_party"] == ResponsibleParty.UNKNOWN


def test_classify_other_buildinputerror():
    """Other BuildInputError (not specific patterns) defaults to configuration error."""
    exc = BuildInputError("Invalid configuration parameter")
    
    result = classify_error(exception=exc)
    
    assert result["category"] == ErrorCategory.CONFIGURATION_ERROR
    assert result["responsible_party"] == ResponsibleParty.APPLICATION_DEVELOPER
    assert result["retryable"] is False
    assert result["user_action_required"] is True


def test_classify_docker_error_non_deploy():
    """DockerServiceError outside deploy is infrastructure error."""
    exc = DockerServiceError("Docker daemon not responding")
    
    result = classify_error(exception=exc, failure_site="validating")
    
    assert result["category"] == ErrorCategory.INFRASTRUCTURE_ERROR
    assert result["responsible_party"] == ResponsibleParty.PLATFORM
    assert result["retryable"] is True
    assert result["user_action_required"] is False
