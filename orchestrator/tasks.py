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
    def dispatch_build_cleanup(self, build_id: str) -> TaskSubmission: ...
    def dispatch_static_check(self, check_id: str, generation: int = 1) -> TaskSubmission: ...
    def dispatch_dynamic_check(self, build_id: str, run_id: str | None = None) -> TaskSubmission: ...


class InMemoryTaskDispatcher:
    """Records messages locally for API and domain tests without a broker."""

    def __init__(self) -> None:
        self.dispatched: list[TaskSubmission] = []
        self.build_ids: list[str] = []
        self.cleanup_build_ids: list[str] = []
        self.static_check_ids: list[str] = []
        self.dynamic_check_ids: list[str] = []
        self.static_check_generations: list[tuple[str, int]] = []
        self.dynamic_check_runs: list[tuple[str, str | None]] = []

    def dispatch_build(self, build_id: str) -> TaskSubmission:
        submission = TaskSubmission(task_id=uuid4().hex, task_name="orchestrator.pipeline.start_build")
        self.dispatched.append(submission)
        self.build_ids.append(build_id)
        return submission

    def dispatch_base_image_sync(self) -> TaskSubmission:
        submission = TaskSubmission(task_id=uuid4().hex, task_name="orchestrator.images.sync_base_images")
        self.dispatched.append(submission)
        return submission

    def dispatch_build_cleanup(self, build_id: str) -> TaskSubmission:
        submission = TaskSubmission(task_id=uuid4().hex, task_name="orchestrator.pipeline.cleanup_build")
        self.dispatched.append(submission)
        self.cleanup_build_ids.append(build_id)
        return submission

    def dispatch_static_check(self, check_id: str, generation: int = 1) -> TaskSubmission:
        submission = TaskSubmission(task_id=uuid4().hex, task_name="orchestrator.pipeline.static_route_check")
        self.dispatched.append(submission)
        self.static_check_ids.append(check_id)
        self.static_check_generations.append((check_id, generation))
        return submission

    def dispatch_dynamic_check(self, build_id: str, run_id: str | None = None) -> TaskSubmission:
        submission = TaskSubmission(task_id=uuid4().hex, task_name="orchestrator.pipeline.dynamic_route_check")
        self.dispatched.append(submission)
        self.dynamic_check_ids.append(build_id)
        self.dynamic_check_runs.append((build_id, run_id))
        return submission
