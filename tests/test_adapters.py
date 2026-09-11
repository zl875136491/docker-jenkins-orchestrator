from orchestrator.adapters import ExternalServiceConfig


def test_external_config_does_not_expose_password() -> None:
    config = ExternalServiceConfig("https://jenkins.example", "worker", "super-secret")
    assert "super-secret" not in repr(config)
    assert config.url.startswith("https://")
