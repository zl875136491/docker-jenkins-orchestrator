import os

from orchestrator.celery_app import create_celery_app


celery_app = create_celery_app(os.getenv("ORCHESTRATOR_CELERY_BROKER_URL", "redis://redis:6379/0"))
