"""Celery task entry points.

The task implementations are registered separately from FastAPI so workers can
run without importing HTTP routes. Business state is always written to MongoDB.
"""

from orchestrator.celery_runtime import celery_app


@celery_app.task(name="orchestrator.pipeline.start_build", bind=True)
def start_build(self, build_id: str) -> dict[str, str]:
    from orchestrator.worker_runtime import get_worker_runtime

    return get_worker_runtime().start_build(build_id, task_id=self.request.id)


@celery_app.task(name="orchestrator.pipeline.poll_build", bind=True)
def poll_build(self, build_id: str, attempt: int = 0) -> dict[str, str]:
    from orchestrator.worker_runtime import get_worker_runtime

    return get_worker_runtime().poll_build(build_id, attempt=attempt, task_id=self.request.id)


@celery_app.task(name="orchestrator.pipeline.recover_builds", bind=True)
def recover_builds(self) -> dict[str, int]:
    from orchestrator.worker_runtime import get_worker_runtime

    return get_worker_runtime().recover_builds(task_id=self.request.id)


@celery_app.task(name="orchestrator.images.sync_base_images", bind=True)
def sync_base_images(self) -> dict[str, int]:
    from orchestrator.worker_runtime import get_worker_runtime

    return get_worker_runtime().sync_base_images(task_id=self.request.id)
