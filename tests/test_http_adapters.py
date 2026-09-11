from orchestrator.http_adapters import HarborHttpAdapter, JsonHttpClient


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        return {"name": "boot-images/python", "id": "service-1", "queue_id": "queue-1"}


def test_adapters_translate_domain_calls_to_http() -> None:
    fake = FakeClient()
    assert HarborHttpAdapter(fake).push_base_image("python:3.12") == "boot-images/python"
    assert fake.calls[0][0:2] == ("POST", "/api/v2.0/projects/boot-images/repositories")


def test_http_client_keeps_auth_header_internal() -> None:
    client = JsonHttpClient("https://example", "user", "password")
    assert client.auth
    assert "password" not in repr(client)
