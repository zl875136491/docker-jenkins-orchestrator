from typing import Protocol

from orchestrator.models import BuildJob, UserApp


class Repository(Protocol):
    def create_app(self, app: UserApp) -> UserApp: ...
    def get_app(self, appid: str) -> UserApp | None: ...
    def create_build(self, build: BuildJob) -> BuildJob: ...
    def get_build(self, build_id: str) -> BuildJob | None: ...


class DuplicateAppError(Exception):
    pass


class InMemoryRepository:
    def __init__(self) -> None:
        self.apps: dict[str, UserApp] = {}
        self.builds: dict[str, BuildJob] = {}

    def create_app(self, app: UserApp) -> UserApp:
        if app.appid in self.apps:
            raise DuplicateAppError(app.appid)
        self.apps[app.appid] = app
        return app

    def get_app(self, appid: str) -> UserApp | None:
        return self.apps.get(appid)

    def create_build(self, build: BuildJob) -> BuildJob:
        self.builds[build.build_id] = build
        return build

    def get_build(self, build_id: str) -> BuildJob | None:
        return self.builds.get(build_id)
