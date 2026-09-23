from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any, Protocol, Sequence

from orchestrator.models import (
    ACTIVE_BUILD_STATUSES,
    Alert,
    AppEvent,
    BaseImage,
    BuildJob,
    BuildStatus,
    DeploymentService,
    TechStack,
    UserAppRecord,
    UserImage,
)


class RepositoryError(RuntimeError):
    pass


class DuplicateAppError(RepositoryError):
    pass


class DuplicateTechStackError(RepositoryError):
    pass


class Repository(Protocol):
    def ensure_indexes(self) -> None: ...
    def close(self) -> None: ...
    def create_app(self, app: UserAppRecord) -> UserAppRecord: ...
    def get_app(self, appid: str) -> UserAppRecord | None: ...
    def list_apps(self) -> list[UserAppRecord]: ...
    def update_app(self, appid: str, fields: dict[str, Any]) -> UserAppRecord | None: ...
    def create_build(self, build: BuildJob) -> BuildJob: ...
    def get_build(self, build_id: str) -> BuildJob | None: ...
    def list_builds(
        self,
        appid: str | None = None,
        status: BuildStatus | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[BuildJob], int]: ...
    def list_active_builds(self) -> list[BuildJob]: ...
    def update_build(self, build_id: str, fields: dict[str, Any]) -> BuildJob | None: ...
    def transition_build(
        self, build_id: str, expected_statuses: Sequence[BuildStatus], status: BuildStatus, fields: dict[str, Any]
    ) -> BuildJob | None: ...
    def append_event(self, event: AppEvent) -> AppEvent: ...
    def list_events(self, appid: str, limit: int = 100) -> list[AppEvent]: ...
    def save_user_image(self, image: UserImage) -> UserImage: ...
    def list_user_images(self, appid: str) -> list[UserImage]: ...
    def save_base_image(self, image: BaseImage) -> BaseImage: ...
    def list_base_images(self) -> list[BaseImage]: ...
    def create_tech_stack(self, stack: TechStack) -> TechStack: ...
    def get_tech_stack(self, tech_stack_id: str) -> TechStack | None: ...
    def list_tech_stacks(self) -> list[TechStack]: ...
    def update_tech_stack(self, tech_stack_id: str, fields: dict[str, Any]) -> TechStack | None: ...
    def delete_tech_stack(self, tech_stack_id: str) -> bool: ...
    def save_service(self, service: DeploymentService) -> DeploymentService: ...
    def list_services(self, appid: str) -> list[DeploymentService]: ...
    def create_alert(self, alert: Alert) -> Alert: ...
    def list_alerts(self, appid: str) -> list[Alert]: ...


class InMemoryRepository:
    """Thread-safe development/test repository mirroring the MongoDB contract."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.apps: dict[str, UserAppRecord] = {}
        self.builds: dict[str, BuildJob] = {}
        self.events: list[AppEvent] = []
        self.user_images: dict[str, UserImage] = {}
        self.base_images: dict[str, BaseImage] = {}
        self.tech_stacks: dict[str, TechStack] = {}
        self.services: dict[str, DeploymentService] = {}
        self.alerts: dict[str, Alert] = {}

    @staticmethod
    def _copy(model):
        return model.model_copy(deep=True)

    def ensure_indexes(self) -> None:
        return None

    def close(self) -> None:
        return None

    def create_app(self, app: UserAppRecord) -> UserAppRecord:
        with self._lock:
            if app.appid in self.apps:
                raise DuplicateAppError(app.appid)
            self.apps[app.appid] = self._copy(app)
            return self._copy(app)

    def get_app(self, appid: str) -> UserAppRecord | None:
        with self._lock:
            app = self.apps.get(appid)
            return self._copy(app) if app else None

    def list_apps(self) -> list[UserAppRecord]:
        with self._lock:
            values = sorted(self.apps.values(), key=lambda app: (app.name.lower(), app.appid))
            return [self._copy(app) for app in values]

    def update_app(self, appid: str, fields: dict[str, Any]) -> UserAppRecord | None:
        with self._lock:
            existing = self.apps.get(appid)
            if existing is None:
                return None
            updated = existing.model_copy(update=fields, deep=True)
            self.apps[appid] = updated
            return self._copy(updated)

    def create_build(self, build: BuildJob) -> BuildJob:
        with self._lock:
            self.builds[build.build_id] = self._copy(build)
            return self._copy(build)

    def get_build(self, build_id: str) -> BuildJob | None:
        with self._lock:
            build = self.builds.get(build_id)
            return self._copy(build) if build else None

    def list_builds(
        self,
        appid: str | None = None,
        status: BuildStatus | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[BuildJob], int]:
        if skip < 0 or limit < 1:
            raise ValueError("skip must be non-negative and limit must be positive")
        with self._lock:
            values = [
                build
                for build in self.builds.values()
                if (appid is None or build.appid == appid)
                and (status is None or build.status == status)
            ]
            values.sort(key=lambda build: (build.created_at, build.build_id), reverse=True)
            total = len(values)
            page = values[skip : skip + limit]
            return [self._copy(build) for build in page], total

    def list_active_builds(self) -> list[BuildJob]:
        with self._lock:
            values = [build for build in self.builds.values() if build.status in ACTIVE_BUILD_STATUSES]
            return [self._copy(build) for build in sorted(values, key=lambda build: build.updated_at)]

    def update_build(self, build_id: str, fields: dict[str, Any]) -> BuildJob | None:
        with self._lock:
            existing = self.builds.get(build_id)
            if existing is None:
                return None
            updated = existing.model_copy(update=fields, deep=True)
            self.builds[build_id] = updated
            return self._copy(updated)

    def transition_build(
        self, build_id: str, expected_statuses: Sequence[BuildStatus], status: BuildStatus, fields: dict[str, Any]
    ) -> BuildJob | None:
        with self._lock:
            existing = self.builds.get(build_id)
            if existing is None or existing.status not in set(expected_statuses):
                return None
            updated = existing.model_copy(update={**fields, "status": status}, deep=True)
            self.builds[build_id] = updated
            return self._copy(updated)

    def append_event(self, event: AppEvent) -> AppEvent:
        with self._lock:
            self.events.append(self._copy(event))
            return self._copy(event)

    def list_events(self, appid: str, limit: int = 100) -> list[AppEvent]:
        with self._lock:
            values = [event for event in self.events if event.appid == appid]
            return [self._copy(event) for event in sorted(values, key=lambda event: event.created_at, reverse=True)[:limit]]

    def save_user_image(self, image: UserImage) -> UserImage:
        with self._lock:
            self.user_images[image.image_id] = self._copy(image)
            return self._copy(image)

    def list_user_images(self, appid: str) -> list[UserImage]:
        with self._lock:
            return [self._copy(image) for image in self.user_images.values() if image.appid == appid]

    def save_base_image(self, image: BaseImage) -> BaseImage:
        with self._lock:
            self.base_images[image.source_image] = self._copy(image)
            return self._copy(image)

    def list_base_images(self) -> list[BaseImage]:
        with self._lock:
            return [self._copy(image) for image in self.base_images.values()]

    def create_tech_stack(self, stack: TechStack) -> TechStack:
        with self._lock:
            if stack.tech_stack_id in self.tech_stacks:
                raise DuplicateTechStackError(stack.tech_stack_id)
            self.tech_stacks[stack.tech_stack_id] = self._copy(stack)
            return self._copy(stack)

    def get_tech_stack(self, tech_stack_id: str) -> TechStack | None:
        with self._lock:
            stack = self.tech_stacks.get(tech_stack_id)
            return self._copy(stack) if stack else None

    def list_tech_stacks(self) -> list[TechStack]:
        with self._lock:
            values = sorted(self.tech_stacks.values(), key=lambda item: (item.name.lower(), item.tech_stack_id))
            return [self._copy(item) for item in values]

    def update_tech_stack(self, tech_stack_id: str, fields: dict[str, Any]) -> TechStack | None:
        with self._lock:
            existing = self.tech_stacks.get(tech_stack_id)
            if existing is None:
                return None
            updated = existing.model_copy(update=fields, deep=True)
            self.tech_stacks[tech_stack_id] = updated
            return self._copy(updated)

    def delete_tech_stack(self, tech_stack_id: str) -> bool:
        with self._lock:
            return self.tech_stacks.pop(tech_stack_id, None) is not None

    def save_service(self, service: DeploymentService) -> DeploymentService:
        with self._lock:
            self.services[service.service_id] = self._copy(service)
            return self._copy(service)

    def list_services(self, appid: str) -> list[DeploymentService]:
        with self._lock:
            return [self._copy(service) for service in self.services.values() if service.appid == appid]

    def create_alert(self, alert: Alert) -> Alert:
        with self._lock:
            self.alerts[alert.alert_id] = self._copy(alert)
            return self._copy(alert)

    def list_alerts(self, appid: str) -> list[Alert]:
        with self._lock:
            return [self._copy(alert) for alert in self.alerts.values() if alert.appid == appid]


class MongoRepository:
    """MongoDB repository. The client is owned by this object when supplied."""

    def __init__(self, database, client=None) -> None:
        self._client = client
        self.apps = database["user_apps"]
        self.builds = database["build_jobs"]
        self.events = database["app_events"]
        self.user_images = database["user_images"]
        self.base_images = database["base_images"]
        self.tech_stacks = database["tech_stacks"]
        self.services = database["deployment_services"]
        self.alerts = database["alerts"]

    @classmethod
    def connect(cls, url: str, database_name: str, timeout_ms: int = 5000) -> "MongoRepository":
        from pymongo import MongoClient

        client = MongoClient(url, serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms)
        repository = cls(client[database_name], client=client)
        repository.ensure_indexes()
        return repository

    @staticmethod
    def _document(model) -> dict[str, Any]:
        return model.model_dump(mode="python")

    @staticmethod
    def _model(model_type, document: dict[str, Any] | None):
        if document is None:
            return None
        document = dict(document)
        document.pop("_id", None)
        return model_type.model_validate(document)

    def ensure_indexes(self) -> None:
        self.apps.create_index("appid", unique=True, name="unique_appid")
        self.builds.create_index("build_id", unique=True, name="unique_build_id")
        self.builds.create_index([("appid", 1), ("status", 1), ("created_at", -1)], name="builds_by_app_status")
        self.builds.create_index([("status", 1), ("updated_at", 1)], name="active_builds_by_status")
        self.events.create_index([("appid", 1), ("created_at", -1)], name="events_by_app")
        self.events.create_index([("build_id", 1), ("created_at", -1)], name="events_by_build")
        self.user_images.create_index([("appid", 1), ("build_id", 1), ("reference", 1)], unique=True, name="unique_user_image")
        self.base_images.create_index("source_image", unique=True, name="unique_base_image")
        self.tech_stacks.create_index("tech_stack_id", unique=True, name="unique_tech_stack_id")
        self.services.create_index([("appid", 1), ("build_id", 1), ("service_name", 1)], unique=True, name="unique_deployment_service")
        self.alerts.create_index([("appid", 1), ("created_at", -1)], name="alerts_by_app")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def create_app(self, app: UserAppRecord) -> UserAppRecord:
        try:
            self.apps.insert_one(self._document(app))
        except Exception as exc:
            if getattr(exc, "code", None) == 11000:
                raise DuplicateAppError(app.appid) from exc
            raise RepositoryError("Unable to create user-app") from exc
        return app

    def get_app(self, appid: str) -> UserAppRecord | None:
        return self._model(UserAppRecord, self.apps.find_one({"appid": appid}))

    def list_apps(self) -> list[UserAppRecord]:
        documents = self.apps.find({}).sort([("name", 1), ("appid", 1)])
        return [self._model(UserAppRecord, document) for document in documents]

    def update_app(self, appid: str, fields: dict[str, Any]) -> UserAppRecord | None:
        from pymongo import ReturnDocument

        document = self.apps.find_one_and_update(
            {"appid": appid}, {"$set": fields}, return_document=ReturnDocument.AFTER
        )
        return self._model(UserAppRecord, document)

    def create_build(self, build: BuildJob) -> BuildJob:
        try:
            self.builds.insert_one(self._document(build))
        except Exception as exc:
            raise RepositoryError("Unable to create build job") from exc
        return build

    def get_build(self, build_id: str) -> BuildJob | None:
        return self._model(BuildJob, self.builds.find_one({"build_id": build_id}))

    def list_builds(
        self,
        appid: str | None = None,
        status: BuildStatus | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[BuildJob], int]:
        if skip < 0 or limit < 1:
            raise ValueError("skip must be non-negative and limit must be positive")
        query: dict[str, Any] = {}
        if appid is not None:
            query["appid"] = appid
        if status is not None:
            query["status"] = status.value
        cursor = self.builds.find(query).sort([("created_at", -1), ("build_id", -1)])
        total = self.builds.count_documents(query)
        documents = cursor.skip(skip).limit(limit)
        return [self._model(BuildJob, document) for document in documents], total

    def list_active_builds(self) -> list[BuildJob]:
        statuses = [status.value for status in ACTIVE_BUILD_STATUSES]
        return [
            self._model(BuildJob, document)
            for document in self.builds.find({"status": {"$in": statuses}}).sort("updated_at", 1)
        ]

    def update_build(self, build_id: str, fields: dict[str, Any]) -> BuildJob | None:
        from pymongo import ReturnDocument

        document = self.builds.find_one_and_update(
            {"build_id": build_id}, {"$set": fields}, return_document=ReturnDocument.AFTER
        )
        return self._model(BuildJob, document)

    def transition_build(
        self, build_id: str, expected_statuses: Sequence[BuildStatus], status: BuildStatus, fields: dict[str, Any]
    ) -> BuildJob | None:
        from pymongo import ReturnDocument

        document = self.builds.find_one_and_update(
            {"build_id": build_id, "status": {"$in": [item.value for item in expected_statuses]}},
            {"$set": {**fields, "status": status.value}},
            return_document=ReturnDocument.AFTER,
        )
        return self._model(BuildJob, document)

    def append_event(self, event: AppEvent) -> AppEvent:
        self.events.insert_one(self._document(event))
        return event

    def list_events(self, appid: str, limit: int = 100) -> list[AppEvent]:
        return [self._model(AppEvent, document) for document in self.events.find({"appid": appid}).sort("created_at", -1).limit(limit)]

    def save_user_image(self, image: UserImage) -> UserImage:
        self.user_images.replace_one(
            {"appid": image.appid, "build_id": image.build_id, "reference": image.reference}, self._document(image), upsert=True
        )
        return image

    def list_user_images(self, appid: str) -> list[UserImage]:
        return [self._model(UserImage, document) for document in self.user_images.find({"appid": appid}).sort("created_at", -1)]

    def save_base_image(self, image: BaseImage) -> BaseImage:
        self.base_images.replace_one({"source_image": image.source_image}, self._document(image), upsert=True)
        return image

    def list_base_images(self) -> list[BaseImage]:
        return [self._model(BaseImage, document) for document in self.base_images.find({}).sort("source_image", 1)]

    def create_tech_stack(self, stack: TechStack) -> TechStack:
        try:
            self.tech_stacks.insert_one(self._document(stack))
        except Exception as exc:
            if getattr(exc, "code", None) == 11000:
                raise DuplicateTechStackError(stack.tech_stack_id) from exc
            raise RepositoryError("Unable to create technology stack") from exc
        return stack

    def get_tech_stack(self, tech_stack_id: str) -> TechStack | None:
        return self._model(TechStack, self.tech_stacks.find_one({"tech_stack_id": tech_stack_id}))

    def list_tech_stacks(self) -> list[TechStack]:
        documents = self.tech_stacks.find({}).sort([("name", 1), ("tech_stack_id", 1)])
        return [self._model(TechStack, document) for document in documents]

    def update_tech_stack(self, tech_stack_id: str, fields: dict[str, Any]) -> TechStack | None:
        from pymongo import ReturnDocument

        document = self.tech_stacks.find_one_and_update(
            {"tech_stack_id": tech_stack_id}, {"$set": fields}, return_document=ReturnDocument.AFTER
        )
        return self._model(TechStack, document)

    def delete_tech_stack(self, tech_stack_id: str) -> bool:
        return self.tech_stacks.delete_one({"tech_stack_id": tech_stack_id}).deleted_count == 1

    def save_service(self, service: DeploymentService) -> DeploymentService:
        self.services.replace_one(
            {"appid": service.appid, "build_id": service.build_id, "service_name": service.service_name},
            self._document(service),
            upsert=True,
        )
        return service

    def list_services(self, appid: str) -> list[DeploymentService]:
        return [self._model(DeploymentService, document) for document in self.services.find({"appid": appid}).sort("created_at", -1)]

    def create_alert(self, alert: Alert) -> Alert:
        self.alerts.insert_one(self._document(alert))
        return alert

    def list_alerts(self, appid: str) -> list[Alert]:
        return [self._model(Alert, document) for document in self.alerts.find({"appid": appid}).sort("created_at", -1)]
