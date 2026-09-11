from typing import Protocol

from orchestrator.models import BuildJob


class TaskDispatcher(Protocol):
    def dispatch_build(self, build: BuildJob) -> None: ...


class InMemoryTaskDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def dispatch_build(self, build: BuildJob) -> None:
        self.dispatched.append(build.build_id)
