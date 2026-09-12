from orchestrator.celery_app import create_celery_app
from orchestrator.config import get_settings


celery_app = create_celery_app(get_settings())
