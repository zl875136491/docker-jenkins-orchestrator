"""Tests for deterministic Compose environment variable validation."""

import pytest

from orchestrator.compose_validator import (
    ComposeEnvironmentValidator,
    format_validation_error,
    MissingVariable,
    VariableReference,
)


def test_extract_references_from_list_environment():
    """Extract references from list-form environment."""
    compose = {
        "services": {
            "web": {
                "environment": [
                    "DATABASE_URL=${DATABASE_URL}",
                    "API_KEY=${API_KEY:?Required}",
                    "DEBUG=${DEBUG:-false}",
                ]
            }
        }
    }
    
    refs = ComposeEnvironmentValidator.extract_references(compose)
    
    assert len(refs) == 3
    assert refs[0].name == "DATABASE_URL"
    assert refs[0].service == "web"
    assert refs[0].has_default is False
    
    assert refs[1].name == "API_KEY"
    assert refs[1].operator == ":?"
    assert refs[1].has_default is False
    
    assert refs[2].name == "DEBUG"
    assert refs[2].operator == ":-"
    assert refs[2].has_default is True


def test_extract_references_from_dict_environment():
    """Extract references from dict-form environment."""
    compose = {
        "services": {
            "api": {
                "environment": {
                    "DB_HOST": "${DB_HOST}",
                    "DB_PORT": "5432",  # static value, no variable
                }
            }
        }
    }
    
    refs = ComposeEnvironmentValidator.extract_references(compose)
    
    assert len(refs) == 1
    assert refs[0].name == "DB_HOST"
    assert refs[0].environment_key == "DB_HOST"


def test_extract_references_multiple_services():
    """Extract references from multiple services."""
    compose = {
        "services": {
            "web": {
                "environment": ["DB_URL=${DB_URL}"]
            },
            "api": {
                "environment": ["DB_URL=${DB_URL}", "API_KEY=${API_KEY}"]
            }
        }
    }
    
    refs = ComposeEnvironmentValidator.extract_references(compose)
    
    # Should return all references, including duplicates
    assert len(refs) == 3
    names = [r.name for r in refs]
    assert names == ["DB_URL", "DB_URL", "API_KEY"]
    
    # Check services
    assert refs[0].service == "web"
    assert refs[1].service == "api"
    assert refs[2].service == "api"


def test_extract_references_supports_bare_syntax():
    """Support both ${VAR} and $VAR syntax."""
    compose = {
        "services": {
            "app": {
                "environment": [
                    "BRACED=${VAR1}",
                    "BARE=$VAR2",
                ]
            }
        }
    }
    
    refs = ComposeEnvironmentValidator.extract_references(compose)
    
    assert len(refs) == 2
    assert refs[0].name == "VAR1"
    assert refs[1].name == "VAR2"


def test_extract_references_ignores_escaped_dollar():
    """$$ should be treated as escaped dollar, not variable."""
    compose = {
        "services": {
            "app": {
                "environment": ["PASSWORD=abc$$123"]
            }
        }
    }
    
    refs = ComposeEnvironmentValidator.extract_references(compose)
    
    assert len(refs) == 0


def test_validate_missing_required_variable():
    """Detect missing required variable."""
    compose = {
        "services": {
            "web": {
                "environment": ["DATABASE_URL=${DATABASE_URL}"]
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    assert len(missing) == 1
    assert missing[0].name == "DATABASE_URL"
    assert missing[0].reason == "missing"
    assert len(missing[0].locations) == 1
    assert missing[0].locations[0]["service"] == "web"


def test_validate_passes_with_configured_variable():
    """Pass when variable is configured."""
    compose = {
        "services": {
            "web": {
                "environment": ["DATABASE_URL=${DATABASE_URL}"]
            }
        }
    }
    environment = {"DATABASE_URL": "postgresql://localhost/db"}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    assert missing == []


def test_validate_allows_default_value():
    """Variable with default value should not fail if missing."""
    compose = {
        "services": {
            "web": {
                "environment": ["DEBUG=${DEBUG:-false}"]
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    assert missing == []


def test_validate_empty_value_with_colon_operator():
    """Empty value should fail with :? operator."""
    compose = {
        "services": {
            "web": {
                "environment": ["API_KEY=${API_KEY:?Required}"]
            }
        }
    }
    environment = {"API_KEY": ""}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    assert len(missing) == 1
    assert missing[0].name == "API_KEY"
    assert missing[0].reason == "empty"


def test_validate_empty_value_without_colon_operator():
    """Empty value should pass with ? operator (no colon)."""
    compose = {
        "services": {
            "web": {
                "environment": ["API_KEY=${API_KEY?Required}"]
            }
        }
    }
    environment = {"API_KEY": ""}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    # ? without : checks existence, not emptiness
    assert missing == []


def test_validate_multiple_missing_variables():
    """Report all missing variables at once."""
    compose = {
        "services": {
            "web": {
                "environment": [
                    "VAR1=${VAR1}",
                    "VAR2=${VAR2}",
                    "VAR3=${VAR3}",
                ]
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    assert len(missing) == 3
    names = [m.name for m in missing]
    assert names == ["VAR1", "VAR2", "VAR3"]


def test_validate_aggregates_same_variable_across_services():
    """Aggregate same variable used in multiple services."""
    compose = {
        "services": {
            "web": {
                "environment": ["DB_URL=${DB_URL}"]
            },
            "api": {
                "environment": ["DB_URL=${DB_URL}"]
            },
            "worker": {
                "environment": ["DB_URL=${DB_URL}"]
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    # Should report once with all locations
    assert len(missing) == 1
    assert missing[0].name == "DB_URL"
    assert len(missing[0].locations) == 3
    services = {loc["service"] for loc in missing[0].locations}
    assert services == {"web", "api", "worker"}


def test_validate_stable_order():
    """Variables should be reported in stable order (first occurrence)."""
    compose = {
        "services": {
            "web": {
                "environment": ["VAR_C=${VAR_C}", "VAR_A=${VAR_A}"]
            },
            "api": {
                "environment": ["VAR_B=${VAR_B}"]
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    names = [m.name for m in missing]
    assert names == ["VAR_C", "VAR_A", "VAR_B"]


def test_validate_mixed_required_and_optional():
    """Handle mix of required and optional variables."""
    compose = {
        "services": {
            "app": {
                "environment": [
                    "REQUIRED=${REQUIRED}",
                    "OPTIONAL=${OPTIONAL:-default}",
                    "ALSO_REQUIRED=${ALSO_REQUIRED:?Must provide}",
                ]
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    assert len(missing) == 2
    names = [m.name for m in missing]
    assert names == ["REQUIRED", "ALSO_REQUIRED"]


def test_format_validation_error():
    """Format validation errors into structured error_detail."""
    missing = [
        MissingVariable(
            name="VAR1",
            reason="missing",
            locations=[{"service": "web", "environment_key": "VAR1", "config_path": "services.web.environment"}],
        ),
        MissingVariable(
            name="VAR2",
            reason="empty",
            locations=[{"service": "api", "environment_key": "VAR2", "config_path": "services.api.environment"}],
        ),
    ]
    
    error_detail = format_validation_error(missing)
    
    assert error_detail["code"] == "COMPOSE_CONFIG_ERROR"
    assert error_detail["subcode"] == "MISSING_ENVIRONMENT_VARIABLES"
    assert "VAR1" in error_detail["summary"]
    assert "VAR2" in error_detail["summary"]
    assert error_detail["recognized"] is True
    assert len(error_detail["missing_variables"]) == 2
    assert error_detail["check_scope"] == "services.environment"
    
    # Check remediation does not leak secrets
    assert "未配置" in error_detail["remediation"]
    assert "配置为空" in error_detail["remediation"]


def test_format_validation_error_no_secret_leakage():
    """Ensure format_validation_error never includes variable values."""
    missing = [
        MissingVariable(
            name="SECRET_KEY",
            reason="missing",
            locations=[{"service": "app", "environment_key": "SECRET_KEY", "config_path": "services.app.environment"}],
            operator=":?",
        ),
    ]
    
    error_detail = format_validation_error(missing)
    
    # Should not contain any actual values or defaults
    detail_str = str(error_detail)
    assert "SECRET_KEY" in detail_str  # Name is OK
    # But no value fields
    assert "value" not in error_detail.get("missing_variables", [{}])[0]
    assert "default" not in error_detail.get("missing_variables", [{}])[0]
    assert "hint" not in error_detail.get("missing_variables", [{}])[0]


def test_validate_handles_null_environment():
    """Handle services without environment field."""
    compose = {
        "services": {
            "web": {
                "image": "nginx"
                # No environment
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    assert missing == []


def test_validate_handles_invalid_environment_gracefully():
    """DockerSwarmAdapter._environment rejects invalid formats, validator skips those services."""
    compose = {
        "services": {
            "valid": {
                "environment": ["VAR=${VAR}"]  # Valid
            },
            "invalid": {
                "environment": 123  # Invalid format - not list or dict
            }
        }
    }
    environment = {}
    
    # Validator should extract from valid service, skip invalid
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    assert len(missing) == 1
    assert missing[0].name == "VAR"
    assert len(missing[0].locations) == 1
    assert missing[0].locations[0]["service"] == "valid"
