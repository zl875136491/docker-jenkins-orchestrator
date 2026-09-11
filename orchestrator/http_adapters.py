import json
from base64 import b64encode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from orchestrator.adapters import AdapterError


class JsonHttpClient:
    def __init__(self, base_url: str, username: str | None = None, password: str | None = None, timeout: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.auth = None
        if username is not None and password is not None:
            self.auth = b64encode(f"{username}:{password}".encode()).decode()

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.auth:
            headers["Authorization"] = f"Basic {self.auth}"
        request = Request(f"{self.base_url}/{path.lstrip('/')}", method=method, headers=headers)
        if payload is not None:
            request.data = json.dumps(payload).encode()
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise AdapterError(f"External request failed: {method} {path}") from exc


class JenkinsHttpAdapter:
    def __init__(self, client: JsonHttpClient) -> None:
        self.client = client

    def trigger_build(self, appid: str, git_ref: str) -> str:
        result = self.client.request("POST", "/job/apps-orchestrator/buildWithParameters", {"APPID": appid, "GIT_REF": git_ref})
        return str(result.get("queue_id", result.get("id", "queued")))

    def get_build_status(self, job_id: str) -> str:
        return str(self.client.request("GET", f"/job/apps-orchestrator/{job_id}/api/json").get("result", "RUNNING"))


class HarborHttpAdapter:
    def __init__(self, client: JsonHttpClient) -> None:
        self.client = client

    def push_base_image(self, image: str) -> str:
        result = self.client.request("POST", "/api/v2.0/projects/boot-images/repositories", {"name": image})
        return str(result.get("name", image))


class DockerServicesHttpAdapter:
    def __init__(self, client: JsonHttpClient) -> None:
        self.client = client

    def deploy_service(self, appid: str, compose: dict) -> str:
        result = self.client.request("POST", "/services", {"appid": appid, "compose": compose})
        return str(result.get("id", appid))
