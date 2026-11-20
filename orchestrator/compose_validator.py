"""Deterministic Compose environment variable validation.

Batch validation for all environment variable references in Compose services,
detecting missing or empty required variables before triggering Jenkins builds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any


# Reuse the same pattern from worker_runtime.py for consistency
_COMPOSE_VARIABLE = re.compile(
    r"\$\$|"
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:(?P<operator>:-|:\?|-|\?)(?P<default>[^}]*))?\}|"
    r"\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)"
)


@dataclass(frozen=True)
class VariableReference:
    """A single environment variable reference in Compose."""
    
    name: str
    service: str
    environment_key: str  # The container env key (e.g., "DATABASE_URL")
    config_path: str      # JSON path (e.g., "services.web.environment")
    operator: str | None  # :-, :?, -, ?
    has_default: bool
    # NOTE: Never store default_value to prevent secret leakage


@dataclass(frozen=True)
class MissingVariable:
    """A variable that failed validation."""
    
    name: str
    reason: str  # "missing" or "empty"
    locations: list[dict[str, str]]  # [{service, environment_key, config_path}, ...]
    operator: str | None = None


class ComposeEnvironmentValidator:
    """Batch validator for Compose environment variables.
    
    Extracts all ${VAR} references from services.*.environment and validates
    against provided application environment without actually interpolating
    (interpolation is deferred to worker runtime).
    """
    
    @staticmethod
    def extract_references(compose: Mapping[str, Any]) -> list[VariableReference]:
        """Extract all environment variable references from Compose.
        
        Returns stable-ordered list of references (deduplicated by identity).
        """
        references: list[VariableReference] = []
        services = compose.get("services") if isinstance(compose, Mapping) else None
        if not isinstance(services, Mapping):
            return references
        
        for service_name, service in services.items():
            if not isinstance(service, Mapping):
                continue
            
            raw_environment = service.get("environment")
            if raw_environment is None:
                continue
            
            # Parse environment entries (supports list and dict forms)
            entries = ComposeEnvironmentValidator._parse_environment(raw_environment)
            
            for entry in entries:
                if "=" not in entry:
                    continue
                key, value = entry.split("=", 1)
                
                # Find all variable references in this value
                for match in _COMPOSE_VARIABLE.finditer(value):
                    if match.group(0) == "$$":
                        continue  # Escaped dollar, not a variable
                    
                    var_name = match.group("braced") or match.group("bare")
                    operator = match.group("operator")
                    has_default = operator in {"-", ":-"} if operator else False
                    
                    references.append(VariableReference(
                        name=var_name,
                        service=service_name,
                        environment_key=key,
                        config_path=f"services.{service_name}.environment",
                        operator=operator,
                        has_default=has_default,
                    ))
        
        return references
    
    @staticmethod
    def _parse_environment(raw: Any) -> list[str]:
        """Parse environment from list or dict form into KEY=VALUE list."""
        if isinstance(raw, list):
            return [str(item) for item in raw if isinstance(item, str)]
        elif isinstance(raw, dict):
            return [f"{k}={v}" for k, v in raw.items() if isinstance(v, str)]
        else:
            return []
    
    @staticmethod
    def validate(
        compose: Mapping[str, Any],
        environment: Mapping[str, str],
    ) -> list[MissingVariable]:
        """Batch validate all environment variables.
        
        Returns list of all missing/empty required variables in stable order.
        Never includes variable values or defaults to prevent secret leakage.
        """
        references = ComposeEnvironmentValidator.extract_references(compose)
        
        # Group by variable name for batch processing
        by_name: dict[str, list[VariableReference]] = {}
        for ref in references:
            by_name.setdefault(ref.name, []).append(ref)
        
        missing: list[MissingVariable] = []
        
        # Check each unique variable (stable order: first occurrence in Compose)
        seen_names: set[str] = set()
        for ref in references:
            if ref.name in seen_names:
                continue
            seen_names.add(ref.name)
            
            refs_for_var = by_name[ref.name]
            
            # Determine if this variable is required
            # Required if ANY reference lacks default or uses :? operator
            any_required = any(
                not r.has_default and r.operator not in {"-", ":-"}
                for r in refs_for_var
            )
            any_empty_required = any(
                r.operator in {":?", ":-"}
                for r in refs_for_var
            )
            
            # Check each reference to determine failure reason
            failure_reason: str | None = None
            if ref.name not in environment:
                # Variable not configured at all
                if any_required:
                    failure_reason = "missing"
            else:
                # Variable configured but may be empty
                value = environment[ref.name]
                if not value and any_empty_required:
                    failure_reason = "empty"
            
            if failure_reason:
                # Collect all locations where this variable is referenced
                locations = [
                    {
                        "service": r.service,
                        "environment_key": r.environment_key,
                        "config_path": r.config_path,
                    }
                    for r in refs_for_var
                ]
                
                # Use operator from first reference that triggered the requirement
                operator = next(
                    (r.operator for r in refs_for_var
                     if (failure_reason == "missing" and not r.has_default)
                     or (failure_reason == "empty" and r.operator in {":?", ":-"})),
                    None
                )
                
                missing.append(MissingVariable(
                    name=ref.name,
                    reason=failure_reason,
                    locations=locations,
                    operator=operator,
                ))
        
        return missing


def format_validation_error(missing: list[MissingVariable]) -> dict[str, Any]:
    """Format validation errors into structured error_detail.
    
    Returns dict suitable for BuildInputError with error_detail attribute.
    Never includes variable values, defaults, or user hints to prevent leakage.
    """
    if not missing:
        return {}
    
    count = len(missing)
    names = [m.name for m in missing]
    
    # User-readable summary
    summary = f"Compose配置缺失{count}个必需的环境变量: {', '.join(names[:5])}"
    if count > 5:
        summary += f" 等{count}个"
    
    # Detailed remediation (never includes actual values or hints)
    remediation_lines = []
    for m in missing:
        services = list({loc["service"] for loc in m.locations})
        if m.reason == "missing":
            remediation_lines.append(
                f"• {m.name} - 未配置 (需要于: {', '.join(services)})"
            )
        else:  # empty
            remediation_lines.append(
                f"• {m.name} - 配置为空但不允许空值 (需要于: {', '.join(services)})"
            )
    
    remediation = "请在应用配置中补充以下环境变量:\n" + "\n".join(remediation_lines)
    
    return {
        "code": "COMPOSE_CONFIG_ERROR",
        "subcode": "MISSING_ENVIRONMENT_VARIABLES",
        "summary": summary,
        "remediation": remediation,
        "recognized": True,
        "missing_variables": [
            {
                "name": m.name,
                "reason": m.reason,
                "locations": m.locations,
                "operator": m.operator,
            }
            for m in missing
        ],
        "check_scope": "services.environment",
    }
