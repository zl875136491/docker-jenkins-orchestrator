from dataclasses import dataclass
from typing import Protocol


class AdapterError(RuntimeError):
    """An external service operation failed without exposing credentials."""


class JenkinsAdapter(Protocol):
    def trigger_build(self, appid: str, git_ref: str) -> str: ...
    def get_build_status(self, job_id: str) -> str: ...


class HarborAdapter(Protocol):
    def push_base_image(self, image: str) -> str: ...


class DockerServicesAdapter(Protocol):
    def deploy_service(self, appid: str, compose: dict) -> str: ...


@dataclass(frozen=True)
class ExternalServiceConfig:
    url: str
    username: str
    password: str

    def __repr__(self) -> str:
        return f"ExternalServiceConfig(url={self.url!r}, username={self.username!r}, password='***')"


class CeleryDispatcher:
    """Optional Celery bridge; importing the package is deferred until dispatch."""

    def __init__(self, broker_url: str, task_name: str = "orchestrator.build") -> None:
        self.broker_url = broker_url
        self.task_name = task_name

    def dispatch_build(self, build) -> None:
        try:
            from celery import Celery
        except ImportError as exc:
            raise AdapterError("Celery is not installed; configure the in-memory dispatcher for development") from exc
        Celery("orchestrator", broker=self.broker_url).send_task(
            self.task_name, args=[build.build_id, build.appid, build.git_ref]
        )
