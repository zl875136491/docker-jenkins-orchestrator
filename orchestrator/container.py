from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator.config import Settings
from orchestrator.gitlab import GitLabHttpAdapter
from orchestrator.repository import InMemoryRepository, MongoRepository, Repository
from orchestrator.secrets import SecretBox
from orchestrator.services import ApplicationService, BuildService, TechStackService
from orchestrator.tasks import InMemoryTaskDispatcher, TaskDispatcher
from orchestrator.templates import TemplateCatalog
from orchestrator.workspaces import WorkspaceStore
from orchestrator.build_resources import BuildResourceService
from orchestrator.route_checks import RouteCheckService
from orchestrator.dynamic_checks import DynamicCheckService
from orchestrator.codex_config import CodexProviderConfigService


@dataclass
class ApplicationContainer:
    settings: Settings
    repository: Repository
    secret_box: SecretBox
    codex_config: CodexProviderConfigService
    applications: ApplicationService
    builds: BuildService
    dispatcher: TaskDispatcher
    catalog: TemplateCatalog
    tech_stacks: TechStackService
    workspace: WorkspaceStore
    build_resources: BuildResourceService
    route_checks: RouteCheckService
    dynamic_checks: DynamicCheckService

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
    codex_config = CodexProviderConfigService(repository, secret_box, settings)
    codex_config.validate_startup(required=settings.environment == "production")
    if dispatcher is None:
        if settings.task_dispatcher == "celery":
            from orchestrator.celery_dispatcher import CeleryTaskDispatcher

            dispatcher = CeleryTaskDispatcher(settings)
        else:
            dispatcher = InMemoryTaskDispatcher()
    if catalog is None:
        catalog = TemplateCatalog(Path(__file__).parents[1] / "templates" / "catalog.yaml")
    workspace = WorkspaceStore.from_settings(settings)
    applications = ApplicationService(repository, secret_box, workspace=workspace)
    if settings.gitlab_url and settings.gitlab_token:
        applications.gitlab = GitLabHttpAdapter.from_settings(settings)
    route_checks = RouteCheckService(
        repository, dispatcher, settings, gitlab_factory=lambda: applications.gitlab,
        codex_config=codex_config,
    )
    applications.route_checks = route_checks
    tech_stacks = TechStackService(repository, catalog)
    builds = BuildService(repository, applications, dispatcher, route_checks=route_checks)
    resources = BuildResourceService(repository, dispatcher, workspace, jenkins_factory=lambda: _jenkins(settings))
    dynamic_checks = DynamicCheckService(repository, dispatcher, settings, route_checks, builds, resources)
    return ApplicationContainer(
        settings=settings,
        repository=repository,
        secret_box=secret_box,
        codex_config=codex_config,
        applications=applications,
        builds=builds,
        dispatcher=dispatcher,
        catalog=catalog,
        tech_stacks=tech_stacks,
        workspace=workspace,
        build_resources=resources,
        route_checks=route_checks,
        dynamic_checks=dynamic_checks,
    )


def _jenkins(settings: Settings):
    from orchestrator.jenkins import JenkinsHttpAdapter

    return JenkinsHttpAdapter.from_settings(settings)
