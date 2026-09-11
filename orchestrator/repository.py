from typing import Protocol

from orchestrator.models import BaseImage, BuildJob, UserApp


class Repository(Protocol):
    def create_app(self, app: UserApp) -> UserApp: ...
    def get_app(self, appid: str) -> UserApp | None: ...
    def create_build(self, build: BuildJob) -> BuildJob: ...
    def get_build(self, build_id: str) -> BuildJob | None: ...
    def save_base_image(self, image: BaseImage) -> BaseImage: ...


class DuplicateAppError(Exception):
    pass


class InMemoryRepository:
    def __init__(self) -> None:
        self.apps: dict[str, UserApp] = {}
        self.builds: dict[str, BuildJob] = {}
        self.base_images: dict[str, BaseImage] = {}

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

    def save_base_image(self, image: BaseImage) -> BaseImage:
        self.base_images[image.image] = image
        return image


class MongoRepository:
    """Mongo-backed repository using small collection methods for easy mocking."""

    def __init__(self, database) -> None:
        self.apps = database["user_apps"]
        self.builds = database["build_jobs"]
        self.base_images = database["base_images"]

    def create_app(self, app: UserApp) -> UserApp:
        try:
            self.apps.insert_one(app.model_dump(mode="json"))
        except Exception as exc:
            if "duplicate" in str(exc).lower() or getattr(exc, "code", None) == 11000:
                raise DuplicateAppError(app.appid) from exc
            raise
        return app

    def get_app(self, appid: str) -> UserApp | None:
        document = self.apps.find_one({"appid": appid})
        return UserApp.model_validate(document) if document else None

    def create_build(self, build: BuildJob) -> BuildJob:
        self.builds.insert_one(build.model_dump(mode="json"))
        return build

    def get_build(self, build_id: str) -> BuildJob | None:
        document = self.builds.find_one({"build_id": build_id})
        return BuildJob.model_validate(document) if document else None

    def save_base_image(self, image: BaseImage) -> BaseImage:
        self.base_images.replace_one({"image": image.image}, image.model_dump(mode="json"), upsert=True)
        return image
