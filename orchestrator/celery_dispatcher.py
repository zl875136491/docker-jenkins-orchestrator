from __future__ import annotations

from typing import Any

from orchestrator.config import Settings
from orchestrator.tasks import TaskSubmission


class CeleryTaskDispatcher:
    """Dispatches durable work to Redis without using a Celery result backend."""

    def __init__(self, settings: Settings, celery_app: Any | None = None) -> None:
        self.settings = settings
        self._celery_app = celery_app

    @property
    def celery_app(self):
        if self._celery_app is None:
            from orchestrator.celery_app import create_celery_app

            self._celery_app = create_celery_app(self.settings)
        return self._celery_app

    def _dispatch(self, task_name: str, queue: str, args: list[Any] | None = None) -> TaskSubmission:
        result = self.celery_app.send_task(task_name, args=args or [], queue=queue)
        return TaskSubmission(task_id=result.id, task_name=task_name)

    def dispatch_build(self, build_id: str) -> TaskSubmission:
        return self._dispatch("orchestrator.pipeline.start_build", self.settings.celery_build_queue, [build_id])

    def dispatch_base_image_sync(self) -> TaskSubmission:
        return self._dispatch("orchestrator.images.sync_base_images", self.settings.celery_images_queue)

    def dispatch_build_cleanup(self, build_id: str) -> TaskSubmission:
        return self._dispatch("orchestrator.pipeline.cleanup_build", self.settings.celery_build_queue, [build_id])

    def dispatch_static_check(self, check_id: str, generation: int = 1) -> TaskSubmission:
        return self._dispatch("orchestrator.pipeline.static_route_check", self.settings.celery_build_queue, [check_id, generation])

    def dispatch_dynamic_check(self, build_id: str, run_id: str | None = None) -> TaskSubmission:
        return self._dispatch("orchestrator.pipeline.dynamic_route_check", self.settings.celery_build_queue, [build_id, run_id])
