from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4


@dataclass(frozen=True)
class TaskSubmission:
    task_id: str
    task_name: str


class TaskDispatcher(Protocol):
    def dispatch_build(self, build_id: str) -> TaskSubmission: ...
    def dispatch_base_image_sync(self) -> TaskSubmission: ...


class InMemoryTaskDispatcher:
    """Records messages locally for API and domain tests without a broker."""

    def __init__(self) -> None:
        self.dispatched: list[TaskSubmission] = []
        self.build_ids: list[str] = []

    def dispatch_build(self, build_id: str) -> TaskSubmission:
        submission = TaskSubmission(task_id=uuid4().hex, task_name="orchestrator.pipeline.start_build")
        self.dispatched.append(submission)
        self.build_ids.append(build_id)
        return submission

    def dispatch_base_image_sync(self) -> TaskSubmission:
        submission = TaskSubmission(task_id=uuid4().hex, task_name="orchestrator.images.sync_base_images")
        self.dispatched.append(submission)
        return submission
