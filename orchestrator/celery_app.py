from __future__ import annotations

from datetime import timedelta

from celery import Celery

from orchestrator.config import Settings


def create_celery_app(settings: Settings) -> Celery:
    """Create the broker-only Celery application used by API, worker, and beat."""
    app = Celery("orchestrator", broker=settings.celery_broker_url, include=["orchestrator.worker_tasks"])
    app.conf.update(
        task_default_queue=settings.celery_build_queue,
        task_routes={
            "orchestrator.pipeline.*": {"queue": settings.celery_build_queue},
            "orchestrator.images.*": {"queue": settings.celery_images_queue},
        },
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        result_backend=None,
        task_track_started=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"visibility_timeout": 60 * 60},
        timezone="UTC",
        enable_utc=True,
        beat_schedule={
            "sync-base-images": {
                "task": "orchestrator.images.sync_base_images",
                "schedule": timedelta(hours=settings.base_image_sync_interval_hours),
                "options": {"queue": settings.celery_images_queue},
            }
        },
    )
    return app
