"""Integration tests for Compose environment variable validation."""

import pytest

from orchestrator.services import BuildInputError
from orchestrator.compose_validator import ComposeEnvironmentValidator


def test_real_case_scm_test3_build_failure():
    """Test real production case: APP-20261118-27C5BCB7 scm-test3.
    
    SHA: ec114efcba9151931c17109c2e1afd90ae637860
    Missing variables: MONGO_URI, MEILI_MASTER_KEY, BUSINESS_MEILI_MASTER_KEY,
                      SESSION_SECRET, BOOTSTRAP_ADMIN_PASSWORD_HASH, EXT_APP_FERNET_KEY
    """
    compose = {
        "services": {
            "server": {
                "environment": [
                    "MONGO_URI=${MONGO_URI:?Required}",
                    "MEILI_MASTER_KEY=${MEILI_MASTER_KEY:?Required}",
                    "BUSINESS_MEILI_MASTER_KEY=${BUSINESS_MEILI_MASTER_KEY:?Required}",
                    "SESSION_SECRET=${SESSION_SECRET:?Required}",
                    "BOOTSTRAP_ADMIN_PASSWORD_HASH=${BOOTSTRAP_ADMIN_PASSWORD_HASH:?Required}",
                    "EXT_APP_FERNET_KEY=${EXT_APP_FERNET_KEY:?Required}",
                ]
            }
        }
    }
    environment = {}  # No variables configured
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    # Should report ALL 6 missing variables at once, not one-at-a-time
    assert len(missing) == 6
    names = {m.name for m in missing}
    assert names == {
        "MONGO_URI",
        "MEILI_MASTER_KEY",
        "BUSINESS_MEILI_MASTER_KEY",
        "SESSION_SECRET",
        "BOOTSTRAP_ADMIN_PASSWORD_HASH",
        "EXT_APP_FERNET_KEY",
    }
    
    # All should be "missing" reason
    for m in missing:
        assert m.reason == "missing"
        assert m.name in [loc["environment_key"] for loc in m.locations]


def test_same_variable_multiple_fields():
    """Test variable referenced in multiple environment fields."""
    compose = {
        "services": {
            "web": {
                "environment": [
                    "DB_URL=${DATABASE_URL}",
                    "CACHE_URL=${DATABASE_URL}",  # Same var, different key
                ]
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    # Should report once
    assert len(missing) == 1
    assert missing[0].name == "DATABASE_URL"
    
    # But should list both locations
    assert len(missing[0].locations) == 2
    keys = {loc["environment_key"] for loc in missing[0].locations}
    assert keys == {"DB_URL", "CACHE_URL"}


def test_same_variable_different_operators():
    """Variable with different operators in different services."""
    compose = {
        "services": {
            "web": {
                "environment": ["VAR=${VAR}"]  # Required
            },
            "api": {
                "environment": ["VAR=${VAR:-default}"]  # Optional
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    # Should require it because web needs it
    assert len(missing) == 1
    assert missing[0].name == "VAR"
    assert len(missing[0].locations) == 2


def test_variable_mapped_to_null():
    """Environment value mapped to null should pass (for compatibility)."""
    compose = {
        "services": {
            "app": {
                "environment": {
                    "VAR": None,  # Null value
                }
            }
        }
    }
    environment = {}
    
    # Should not crash, should skip null values
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    assert missing == []


def test_variable_mapped_to_non_string():
    """Environment value mapped to non-string should be skipped."""
    compose = {
        "services": {
            "app": {
                "environment": {
                    "PORT": 8080,  # Number instead of string
                    "DEBUG": True,  # Boolean instead of string
                }
            }
        }
    }
    environment = {}
    
    # Should not crash
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    assert missing == []


def test_default_value_not_evaluated():
    """Default values should not be leaked in validation."""
    compose = {
        "services": {
            "app": {
                "environment": ["SECRET=${SECRET:-supersecretdefault}"]
            }
        }
    }
    environment = {}
    
    # Should pass (has default)
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    assert missing == []
    
    # Validation should NEVER return the default value


def test_empty_vs_missing_distinction():
    """Distinguish between empty and missing variables."""
    compose = {
        "services": {
            "app": {
                "environment": [
                    "VAR1=${VAR1:?}",  # Requires non-empty
                    "VAR2=${VAR2?}",  # Requires existence only
                ]
            }
        }
    }
    environment = {"VAR1": "", "VAR2": ""}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    # Only VAR1 should fail (empty with :?)
    assert len(missing) == 1
    assert missing[0].name == "VAR1"
    assert missing[0].reason == "empty"


def test_multiple_services_same_missing_variable():
    """Same variable missing across multiple services."""
    compose = {
        "services": {
            "web": {
                "environment": ["DATABASE_URL=${DATABASE_URL}"]
            },
            "worker": {
                "environment": ["DATABASE_URL=${DATABASE_URL}"]
            },
            "scheduler": {
                "environment": ["DATABASE_URL=${DATABASE_URL}"]
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    # Report once with all locations
    assert len(missing) == 1
    assert missing[0].name == "DATABASE_URL"
    assert len(missing[0].locations) == 3
    
    services = {loc["service"] for loc in missing[0].locations}
    assert services == {"web", "worker", "scheduler"}


def test_bare_dollar_syntax():
    """Support $VAR syntax (no braces)."""
    compose = {
        "services": {
            "app": {
                "environment": [
                    "HOME=$HOME",
                    "PATH=$PATH",
                ]
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    
    assert len(missing) == 2
    names = {m.name for m in missing}
    assert names == {"HOME", "PATH"}


def test_escaped_dollar():
    """$$ should be treated as literal dollar, not variable."""
    compose = {
        "services": {
            "app": {
                "environment": [
                    "PASSWORD=abc$$123",  # $$ = literal $
                    "REGEX=\\$$[0-9]+",
                ]
            }
        }
    }
    environment = {}
    
    # No variables to validate
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    assert missing == []


def test_build_input_error_with_error_detail():
    """BuildInputError should carry error_detail attribute."""
    try:
        raise BuildInputError(
            "Test error",
            error_detail={"code": "TEST", "summary": "Test"}
        )
    except BuildInputError as exc:
        assert hasattr(exc, "error_detail")
        assert exc.error_detail["code"] == "TEST"
        assert exc.error_detail["summary"] == "Test"


def test_no_services_returns_empty():
    """Compose without services should return no missing variables."""
    compose = {}
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    assert missing == []


def test_service_without_environment():
    """Service without environment field should be OK."""
    compose = {
        "services": {
            "web": {
                "image": "nginx"
            }
        }
    }
    environment = {}
    
    missing = ComposeEnvironmentValidator.validate(compose, environment)
    assert missing == []
