from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from orchestrator.config import Settings
from orchestrator.secrets import SecretBox, SecretError


class CodexConfigurationError(RuntimeError):
    pass


ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


class CodexProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_name: str = Field(min_length=1, max_length=128)
    provider_endpoint: str = Field(min_length=8, max_length=2048)
    api_key: SecretStr = Field(repr=False)
    default_model: str = Field(min_length=1, max_length=128)
    default_effort: ReasoningEffort
    provider_id: str = Field(default="department-relay", min_length=1, max_length=64)
    wire_api: Literal["responses"] = "responses"
    requires_openai_auth: bool = False

    @field_validator("provider_name", "default_model", mode="before")
    @classmethod
    def trim_required_strings(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("api_key", mode="before")
    @classmethod
    def reject_empty_api_key(cls, value):
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("API key is required")
        return value

    @field_validator("provider_endpoint")
    @classmethod
    def validate_provider_endpoint(cls, value: str) -> str:
        value = value.strip()
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535)
                and not any(ord(character) <= 32 for character in value)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Provider endpoint must be an absolute HTTP(S) URL without credentials, query, or fragment")
        return value.rstrip("/")

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
            raise ValueError("Provider ID must be a TOML-safe identifier")
        return value


class CodexProviderConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_name: str | None = Field(default=None, min_length=1, max_length=128)
    provider_endpoint: str | None = Field(default=None, min_length=8, max_length=2048)
    api_key: SecretStr | None = Field(default=None, repr=False)
    default_model: str | None = Field(default=None, min_length=1, max_length=128)
    default_effort: ReasoningEffort | None = None
    provider_id: str | None = Field(default=None, min_length=1, max_length=64)
    wire_api: Literal["responses"] | None = None
    requires_openai_auth: bool | None = None

    @model_validator(mode="after")
    def require_fields(self) -> "CodexProviderConfigUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one Codex provider field must be provided")
        return self


class CodexProviderConfigView(BaseModel):
    provider_name: str
    provider_endpoint: str
    default_model: str
    default_effort: ReasoningEffort
    provider_id: str
    wire_api: Literal["responses"]
    requires_openai_auth: bool
    api_key_configured: bool
    source: Literal["database", "environment"]


class CodexProviderConfigService:
    def __init__(self, repository: Any, secret_box: SecretBox, settings: Settings) -> None:
        self.repository = repository
        self.secret_box = secret_box
        self.settings = settings

    def _from_database(self) -> CodexProviderConfig | None:
        record = self.repository.get_codex_provider_config()
        if not isinstance(record, dict):
            return None
        try:
            encrypted_key = record.pop("api_key_ciphertext")
            secret = self.secret_box.decrypt_environment(encrypted_key)
            values = dict(record)
            values["api_key"] = secret["api_key"]
            return CodexProviderConfig.model_validate(values)
        except (KeyError, TypeError, ValueError, SecretError):
            return None

    def _from_environment(self) -> CodexProviderConfig | None:
        api_key = self.settings.codex_api_key
        values = {
            "provider_name": self.settings.codex_provider_name,
            "provider_endpoint": self.settings.codex_provider_endpoint,
            "api_key": api_key.get_secret_value() if api_key is not None else None,
            "default_model": self.settings.codex_default_model,
            "default_effort": self.settings.codex_default_effort,
            "provider_id": self.settings.codex_provider_id,
            "wire_api": self.settings.codex_provider_wire_api,
            "requires_openai_auth": self.settings.codex_provider_requires_openai_auth,
        }
        try:
            return CodexProviderConfig.model_validate(values)
        except ValueError:
            return None

    def resolve(self, *, required: bool = True) -> tuple[CodexProviderConfig | None, Literal["database", "environment"] | None]:
        stored = self._from_database()
        if stored is not None:
            return stored, "database"
        from_environment = self._from_environment()
        if from_environment is not None:
            return from_environment, "environment"
        if not required:
            return None, None
        raise CodexConfigurationError(
            "Codex provider configuration is missing or invalid; configure a valid database record or CODEX_PROVIDER_NAME, "
            "CODEX_PROVIDER_ENDPOINT, CODEX_API_KEY, CODEX_DEFAULT_MODEL, and CODEX_DEFAULT_EFFORT"
        )

    def validate_startup(self, *, required: bool) -> None:
        config, source = self.resolve(required=required)
        if config is not None and source == "environment" and self.settings.environment == "production":
            self._save(config)

    def view(self, *, required: bool = True) -> CodexProviderConfigView:
        config, source = self.resolve(required=required)
        if config is None or source is None:
            raise CodexConfigurationError("Codex provider configuration is not available")
        return CodexProviderConfigView(
            provider_name=config.provider_name,
            provider_endpoint=config.provider_endpoint,
            default_model=config.default_model,
            default_effort=config.default_effort,
            provider_id=config.provider_id,
            wire_api=config.wire_api,
            requires_openai_auth=config.requires_openai_auth,
            api_key_configured=True,
            source=source,
        )

    def update(self, update: CodexProviderConfigUpdate) -> CodexProviderConfigView:
        current, _ = self.resolve(required=True)
        values = current.model_dump()
        provided = update.model_dump(exclude_unset=True)
        new_key = provided.pop("api_key", None)
        for field, value in provided.items():
            if value is not None:
                values[field] = value
        if new_key is not None and new_key.get_secret_value().strip():
            values["api_key"] = new_key
        config = CodexProviderConfig.model_validate(values)
        self._save(config)
        return self.view()

    def _save(self, config: CodexProviderConfig) -> None:
        document = config.model_dump(exclude={"api_key"})
        document["api_key_ciphertext"] = self.secret_box.encrypt_environment({
            "api_key": config.api_key.get_secret_value(),
        })
        self.repository.save_codex_provider_config(document)


def codex_cli_overrides(config: CodexProviderConfig) -> list[str]:
    import json

    prefix = f"model_providers.{config.provider_id}"
    values = {
        "model_provider": config.provider_id,
        f"{prefix}.name": config.provider_name,
        f"{prefix}.base_url": config.provider_endpoint,
        f"{prefix}.env_key": "CODEX_API_KEY",
        f"{prefix}.wire_api": config.wire_api,
        f"{prefix}.requires_openai_auth": config.requires_openai_auth,
    }
    return [item for key, value in values.items() for item in ("-c", f"{key}={json.dumps(value, ensure_ascii=False)}")]
