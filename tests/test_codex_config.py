from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from main import create_app
from orchestrator.agent_executor import CodexCliExecutor
from orchestrator.codex_config import (
    CodexConfigurationError,
    CodexProviderConfigService,
    CodexProviderConfigUpdate,
)
from orchestrator.config import Settings
from orchestrator.container import create_container
from orchestrator.models import AgentRole, AgentRuntimeConfig
from orchestrator.repository import InMemoryRepository
from orchestrator.repository import MongoRepository
from orchestrator.secrets import SecretBox


def settings(**overrides) -> Settings:
    values = {
        "environment": "test",
        "worker_name": "test-worker",
        "worker_secret": "test-worker-secret",
        "data_encryption_key": Fernet.generate_key().decode(),
        "codex_provider_name": "测试中转",
        "codex_provider_endpoint": "https://relay.example.test/v1",
        "codex_api_key": "env-test-key",
        "codex_default_model": "gpt-test",
        "codex_default_effort": "ultra",
    }
    values.update(overrides)
    return Settings(**values)


def test_codex_config_loads_from_environment_and_requires_complete_valid_values():
    repository = InMemoryRepository()
    service = CodexProviderConfigService(repository, SecretBox(None), settings())

    config, source = service.resolve()

    assert source == "environment"
    assert config.provider_name == "测试中转"
    assert config.default_effort == "ultra"
    assert config.api_key.get_secret_value() == "env-test-key"

    invalid = CodexProviderConfigService(
        repository, SecretBox(None), settings(codex_provider_endpoint="http:/"),
    )
    with pytest.raises(CodexConfigurationError, match="CODEX_PROVIDER_ENDPOINT"):
        invalid.validate_startup(required=True)


def test_codex_settings_read_exact_codex_environment_names(monkeypatch):
    for key, value in {
        "CODEX_PROVIDER_NAME": "环境中转",
        "CODEX_PROVIDER_ENDPOINT": "https://relay.example.test/v1",
        "CODEX_API_KEY": "env-secret-value",
        "CODEX_DEFAULT_MODEL": "gpt-env",
        "CODEX_DEFAULT_EFFORT": "high",
    }.items():
        monkeypatch.setenv(key, value)

    configured = Settings(_env_file=None)

    assert configured.codex_provider_name == "环境中转"
    assert configured.codex_provider_endpoint == "https://relay.example.test/v1"
    assert configured.codex_api_key.get_secret_value() == "env-secret-value"
    assert configured.codex_default_model == "gpt-env"
    assert configured.codex_default_effort == "high"


def test_database_config_takes_precedence_and_api_key_is_encrypted():
    repository = InMemoryRepository()
    secret_box = SecretBox(Fernet.generate_key().decode())
    service = CodexProviderConfigService(repository, secret_box, settings())
    service.update(CodexProviderConfigUpdate(
        provider_name="数据库中转", api_key="database-test-key",
    ))

    record = repository.get_codex_provider_config()
    assert "database-test-key" not in repr(record)
    assert "api_key" not in record
    assert "api_key_ciphertext" in record
    config, source = service.resolve()
    assert source == "database"
    assert config.api_key.get_secret_value() == "database-test-key"


def test_invalid_database_config_falls_back_to_environment():
    repository = InMemoryRepository()
    repository.save_codex_provider_config({"provider_name": "incomplete"})
    service = CodexProviderConfigService(repository, SecretBox(None), settings())

    config, source = service.resolve()

    assert source == "environment"
    assert config.provider_name == "测试中转"


def test_mongo_repository_persists_the_single_codex_configuration_record():
    class ConfigCollection:
        document = None

        def find_one(self, query):
            if self.document is None or self.document["_id"] != query["_id"]:
                return None
            return dict(self.document)

        def replace_one(self, query, document, *, upsert):
            assert query == {"_id": "codex-provider"}
            assert upsert is True
            self.document = dict(document)

    collection = ConfigCollection()
    database = {name: object() for name in (
        "user_apps", "build_jobs", "app_events", "user_images", "base_images", "tech_stacks",
        "deployment_services", "alerts", "build_resources", "port_allocations", "agent_roles", "route_check_list",
    )}
    database["system_configs"] = collection
    repository = MongoRepository(database)
    service = CodexProviderConfigService(repository, SecretBox(Fernet.generate_key().decode()), settings())

    service.update(CodexProviderConfigUpdate(api_key="mongo-encrypted-key"))

    config, source = service.resolve()
    assert source == "database"
    assert config.api_key.get_secret_value() == "mongo-encrypted-key"
    assert "mongo-encrypted-key" not in repr(collection.document)


def test_startup_validation_rejects_missing_or_invalid_environment_config():
    service = CodexProviderConfigService(
        InMemoryRepository(), SecretBox(None), settings(codex_api_key=None),
    )
    with pytest.raises(CodexConfigurationError):
        service.validate_startup(required=True)


def test_codex_configuration_page_is_in_global_navigation():
    from main import app

    with TestClient(app) as client:
        page = client.get("/ui/")
        script = client.get("/ui/app.js")
        stylesheet = client.get("/ui/styles.css")

    assert 'href="#codexSettingsSection"' in page.text
    assert 'id="codexConfigForm"' in page.text
    assert 'id="codexApiKey"' in page.text
    assert 'id="codexConfigSource"' in page.text
    assert "/api/v1/system/codex-config" in script.text
    assert "api_key_configured" in script.text
    assert ".codex-config-grid" in stylesheet.text


def test_codex_config_api_masks_key_and_persists_updates():
    configured = settings()
    container = create_container(configured)
    app = create_app(settings=configured, container=container)
    with TestClient(app) as client:
        token = client.post("/oauth2/token", json={
            "client_id": "test-worker", "client_secret": "test-worker-secret",
        }).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        assert client.get("/api/v1/system/codex-config").status_code == 401
        response = client.get("/api/v1/system/codex-config", headers=headers)
        assert response.status_code == 200
        assert response.json()["api_key_configured"] is True
        assert "api_key" not in response.json()
        assert "env-test-key" not in response.text

        updated = client.put("/api/v1/system/codex-config", headers=headers, json={
            "provider_name": "网页修改中转",
            "api_key": "rotated-test-key",
        })
        assert updated.status_code == 200
        assert updated.json()["provider_name"] == "网页修改中转"
        assert "rotated-test-key" not in updated.text
        current, source = container.codex_config.resolve()
        assert source == "database"
        assert current.api_key.get_secret_value() == "rotated-test-key"


class CliRunner:
    def __init__(self) -> None:
        self.command = []
        self.environment = {}

    def __call__(self, command, **kwargs):
        self.command = command
        self.environment = kwargs["env"]
        output_file = Path(command[command.index("--output-last-message") + 1])
        output_file.write_text(json.dumps({"response_json": '{"ok":true}'}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)


def test_cli_uses_database_provider_and_environment_key_without_argv_leak(tmp_path):
    repository = InMemoryRepository()
    secret_box = SecretBox(Fernet.generate_key().decode())
    service = CodexProviderConfigService(repository, secret_box, settings())
    service.update(CodexProviderConfigUpdate(api_key="database-cli-key"))
    runner = CliRunner()
    role = AgentRole(
        role_key="static_build_agent",
        prompt="Return JSON",
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                       "required": ["ok"], "additionalProperties": False},
        runtime_config=AgentRuntimeConfig(),
    )

    result = CodexCliExecutor(
        sys.executable, runner=runner, provider_config=service, require_provider=True,
    ).run_json(role, {}, cwd=tmp_path)

    assert result == {"ok": True}
    assert runner.environment["CODEX_API_KEY"] == "database-cli-key"
    assert runner.environment["CODEX_API_KEY"] != "env-test-key"
    assert "database-cli-key" not in " ".join(runner.command)
    assert "--model" in runner.command
    assert runner.command[runner.command.index("--model") + 1] == "gpt-test"
    assert 'model_reasoning_effort="ultra"' in runner.command
    assert "model_providers.department-relay.base_url=\"https://relay.example.test/v1\"" in runner.command
