from types import SimpleNamespace

from orchestrator.celery_app import create_celery_app
from orchestrator.celery_dispatcher import CeleryTaskDispatcher
from orchestrator.config import Settings


class FakeCelery:
    def __init__(self) -> None:
        self.calls = []

    def send_task(self, name, args, queue):
        self.calls.append((name, args, queue))
        return SimpleNamespace(id="celery-task-id")


def test_celery_dispatcher_routes_build_and_image_tasks_to_separate_queues() -> None:
    settings = Settings(task_dispatcher="celery", celery_build_queue="builds", celery_images_queue="images")
    fake = FakeCelery()
    dispatcher = CeleryTaskDispatcher(settings, celery_app=fake)

    assert dispatcher.dispatch_build("build-1").task_id == "celery-task-id"
    assert dispatcher.dispatch_base_image_sync().task_name == "orchestrator.images.sync_base_images"
    assert fake.calls == [
        ("orchestrator.pipeline.start_build", ["build-1"], "builds"),
        ("orchestrator.images.sync_base_images", [], "images"),
    ]


def test_celery_uses_redis_broker_without_result_backend() -> None:
    settings = Settings(
        celery_broker_url="redis://redis:6379/0",
        base_image_sync_interval_hours=12,
        celery_recovery_interval_seconds=120,
    )
    app = create_celery_app(settings)
    assert app.conf.broker_url == "redis://redis:6379/0"
    assert app.conf.result_backend is None
    assert "sync-base-images" in app.conf.beat_schedule
    assert "recover-incomplete-builds" in app.conf.beat_schedule
    assert app.conf.beat_schedule["recover-incomplete-builds"]["options"]["queue"] == settings.celery_build_queue
