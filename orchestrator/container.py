from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator.config import Settings
from orchestrator.repository import InMemoryRepository, MongoRepository, Repository
from orchestrator.secrets import SecretBox
from orchestrator.services import ApplicationService, BuildService, TechStackService
from orchestrator.tasks import InMemoryTaskDispatcher, TaskDispatcher
from orchestrator.templates import TemplateCatalog


@dataclass
class ApplicationContainer:
    settings: Settings
    repository: Repository
    secret_box: SecretBox
    applications: ApplicationService
    builds: BuildService
    dispatcher: TaskDispatcher
    catalog: TemplateCatalog
    tech_stacks: TechStackService

    def close(self) -> None:
        self.repository.close()


def create_container(
    settings: Settings,
    repository: Repository | None = None,
    dispatcher: TaskDispatcher | None = None,
    catalog: TemplateCatalog | None = None,
) -> ApplicationContainer:
    if repository is None:
        if settings.storage_backend == "mongo":
            repository = MongoRepository.connect(settings.mongodb_url, settings.mongodb_database, settings.mongodb_timeout_ms)
        else:
            repository = InMemoryRepository()
    repository.ensure_indexes()
    secret_box = SecretBox(settings.data_encryption_key, require_encryption=settings.storage_backend == "mongo")
    if dispatcher is None:
        if settings.task_dispatcher == "celery":
            from orchestrator.celery_dispatcher import CeleryTaskDispatcher

            dispatcher = CeleryTaskDispatcher(settings)
        else:
            dispatcher = InMemoryTaskDispatcher()
    if catalog is None:
        catalog = TemplateCatalog(Path(__file__).parents[1] / "templates" / "catalog.yaml")
    applications = ApplicationService(repository, secret_box)
    tech_stacks = TechStackService(repository, catalog)
    return ApplicationContainer(
        settings=settings,
        repository=repository,
        secret_box=secret_box,
        applications=applications,
        builds=BuildService(repository, applications, dispatcher),
        dispatcher=dispatcher,
        catalog=catalog,
        tech_stacks=tech_stacks,
    )
