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


@celery_app.task(name="orchestrator.pipeline.cleanup_build", bind=True)
def cleanup_build(self, build_id: str) -> dict[str, str]:
    from orchestrator.worker_runtime import get_worker_runtime

    return get_worker_runtime().cleanup_build(build_id)


@celery_app.task(name="orchestrator.pipeline.recover_cleanups")
def recover_cleanups() -> dict[str, int]:
    from orchestrator.worker_runtime import get_worker_runtime

    return get_worker_runtime().recover_cleanups()


@celery_app.task(name="orchestrator.pipeline.recover_builds", bind=True)
def recover_builds(self) -> dict[str, int]:
    from orchestrator.worker_runtime import get_worker_runtime

    return get_worker_runtime().recover_builds(task_id=self.request.id)


@celery_app.task(name="orchestrator.images.sync_base_images", bind=True)
def sync_base_images(self) -> dict[str, int]:
    from orchestrator.worker_runtime import get_worker_runtime

    return get_worker_runtime().sync_base_images(task_id=self.request.id)


@celery_app.task(name="orchestrator.pipeline.static_route_check", bind=True)
def static_route_check(self, check_id: str, generation: int | None = None) -> dict[str, str]:
    from orchestrator.worker_runtime import get_worker_runtime

    return get_worker_runtime().static_route_check(check_id, generation)


@celery_app.task(name="orchestrator.pipeline.dynamic_route_check", bind=True)
def dynamic_route_check(self, build_id: str, run_id: str | None = None) -> dict[str, str]:
    from orchestrator.worker_runtime import get_worker_runtime

    return get_worker_runtime().dynamic_route_check(build_id, run_id)


@celery_app.task(name="orchestrator.pipeline.recover_route_checks")
def recover_route_checks() -> dict[str, int]:
    from orchestrator.worker_runtime import get_worker_runtime

    return get_worker_runtime().recover_route_checks()
