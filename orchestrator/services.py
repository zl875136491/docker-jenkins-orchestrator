from __future__ import annotations

from datetime import datetime
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import uuid4

import yaml

from orchestrator.models import (
    Alert,
    AppEvent,
    BuildCreate,
    BuildJob,
    BuildStatus,
    EventLevel,
    TERMINAL_BUILD_STATUSES,
    UserApp,
    UserAppCreate,
    UserAppRecord,
    UserAppUpdate,
    TechStack,
    TechStackCreate,
    TechStackUpdate,
    json_structure_paths,
    utc_now,
)
from orchestrator.repository import DuplicateAppError, DuplicateTechStackError, Repository
from orchestrator.secrets import SecretBox
from orchestrator.tasks import TaskDispatcher
from orchestrator.templates import TemplateError


class NotFoundError(LookupError):
    pass


class BuildInputError(ValueError):
    pass


class InvalidBuildTransition(ValueError):
    pass


ALLOWED_BUILD_TRANSITIONS: dict[BuildStatus, set[BuildStatus]] = {
    BuildStatus.QUEUED: {BuildStatus.VALIDATING, BuildStatus.FAILED, BuildStatus.CANCELLED},
    BuildStatus.VALIDATING: {BuildStatus.TRIGGERING, BuildStatus.FAILED, BuildStatus.CANCELLED},
    BuildStatus.TRIGGERING: {BuildStatus.BUILDING, BuildStatus.FAILED, BuildStatus.CANCELLED},
    BuildStatus.BUILDING: {BuildStatus.DEPLOYING, BuildStatus.FAILED, BuildStatus.CANCELLED},
    BuildStatus.DEPLOYING: {BuildStatus.SUCCEEDED, BuildStatus.FAILED, BuildStatus.CANCELLED},
    BuildStatus.SUCCEEDED: set(),
    BuildStatus.FAILED: set(),
    BuildStatus.CANCELLED: set(),
}


class ApplicationService:
    def __init__(self, repository: Repository, secret_box: SecretBox) -> None:
        self.repository = repository
        self.secret_box = secret_box

    @staticmethod
    def _public(record: UserAppRecord) -> UserApp:
        return UserApp.from_record(record)

    def create_app(self, request: UserAppCreate) -> UserApp:
        now = utc_now()
        record = UserAppRecord(
            appid=request.appid,
            name=request.name,
            repository_url=request.repository_url,
            git_ref=request.git_ref,
            environment_ciphertext=self.secret_box.encrypt_environment(request.environment),
            environment_keys=sorted(request.environment),
            compose=request.compose,
            components=request.components,
            created_at=now,
            updated_at=now,
        )
        return self._public(self.repository.create_app(record))

    def get_record(self, appid: str) -> UserAppRecord:
        app = self.repository.get_app(appid)
        if app is None:
            raise NotFoundError("App not found")
        return app

    def get_app(self, appid: str) -> UserApp:
        return self._public(self.get_record(appid))

    def list_apps(self) -> list[UserApp]:
        return [self._public(record) for record in self.repository.list_apps()]

    def get_environment(self, appid: str) -> dict[str, str]:
        return self.secret_box.decrypt_environment(self.get_record(appid).environment_ciphertext)

    def update_app(self, appid: str, request: UserAppUpdate) -> UserApp:
        self.get_record(appid)
        fields = request.model_dump(exclude_unset=True)
        if "environment" in fields:
            environment = fields.pop("environment")
            if environment is not None:
                fields["environment_ciphertext"] = self.secret_box.encrypt_environment(environment)
                fields["environment_keys"] = sorted(environment)
        fields["updated_at"] = utc_now()
        updated = self.repository.update_app(appid, fields)
        if updated is None:
            raise NotFoundError("App not found")
        return self._public(updated)


class TechStackService:
    """Persist and project editable stack records into the compose catalog."""

    def __init__(self, repository: Repository, catalog) -> None:
        self.repository = repository
        self.catalog = catalog
        self._synchronize_catalog()

    @staticmethod
    def _comments(json_data: dict[str, Any], comments: Mapping[str, str] | None) -> dict[str, str]:
        comments = comments or {}
        paths = json_structure_paths(json_data)
        unknown = sorted(set(comments) - set(paths))
        if unknown:
            raise TemplateError(f"line_comments contains unknown JSON paths: {', '.join(unknown)}")
        return {path: str(comments.get(path, "")) for path in paths}

    @staticmethod
    def _default_comment(tech_stack_id: str, path: str, value: Any) -> str:
        """Describe a catalog field so seeded and migrated records are self-explanatory."""

        if path == "$":
            return f"{tech_stack_id} 技术栈的完整组件配置；组合 Compose 时按项目实际需求核对。"
        if path == "$.images":
            return "可选的固定版本基础镜像列表；交付时选择与项目运行时兼容的一项。"
        if path.startswith("$.images["):
            return f"候选基础镜像 {value}；使用前确认架构、运行时版本和安全更新要求。"
        if path == "$.port":
            return "服务在容器内监听的主端口；必须与应用实际监听端口一致。"
        if path == "$.ports":
            return "服务需要监听的容器端口列表；逐项确认用途以及是否需要公开。"
        if path.startswith("$.ports["):
            return f"容器监听端口 {value}；仅在需要外部访问时映射宿主机端口。"
        if path == "$.publish_ports":
            return "是否为模板服务生成宿主机端口映射；数据库和内部依赖通常保持关闭。"
        if path == "$.working_dir":
            return "容器内应用工作目录；启动命令和相对路径均以此目录为基准。"
        if path == "$.restart":
            return "容器重启策略；应符合目标环境的故障恢复和人工停机要求。"
        if path == "$.command":
            return "覆盖镜像默认启动命令的参数列表；修改前核对镜像入口点。"
        if path.startswith("$.command["):
            return f"启动命令参数 `{value}`；顺序会影响容器的实际启动行为。"
        if path == "$.environment":
            return "服务运行环境变量；秘密必须通过部署环境注入，不能保存真实值。"
        if path.startswith("$.environment."):
            variable = path.rsplit(".", 1)[-1]
            if any(marker in variable.upper() for marker in ("PASSWORD", "SECRET", "TOKEN", "KEY")):
                return f"敏感环境变量 {variable}；必须保留变量引用并由部署环境安全注入。"
            return f"环境变量 {variable}；根据项目源码和部署环境确认最终值。"
        if path == "$.volumes":
            return "持久化挂载列表；确保有状态数据在容器重建后仍然保留。"
        if ".volumes[" in path and path.endswith(".source"):
            return "命名卷的逻辑名称；组合时会按应用和服务生成唯一卷名。"
        if ".volumes[" in path and path.endswith(".target"):
            return f"容器内持久化目录 {value}；必须与镜像实际数据目录一致。"
        if path.startswith("$.volumes["):
            return "一项持久化卷挂载配置，包含卷来源和容器内目标目录。"
        if path == "$.healthcheck":
            return "服务就绪探针；依赖方可据此等待服务真正可用。"
        if path == "$.healthcheck.test":
            return "健康检查命令及参数；应验证真实服务协议而不只是进程存在。"
        if path.startswith("$.healthcheck.test["):
            return f"健康检查命令片段 `{value}`；修改后需在目标镜像中验证可执行性。"
        if path == "$.healthcheck.interval":
            return "两次健康检查之间的时间间隔。"
        if path == "$.healthcheck.timeout":
            return "单次健康检查允许的最长执行时间。"
        if path == "$.healthcheck.retries":
            return "连续检查失败多少次后将服务标记为不健康。"
        if path == "$.healthcheck.start_period":
            return "容器启动后的健康检查宽限期，应覆盖服务正常初始化时间。"
        if path == "$.connection":
            return "供其他模板服务引用的连接信息生成规则。"
        if path == "$.connection.environment":
            return "依赖此服务的容器将获得的连接环境变量模板。"
        if path.startswith("$.connection.environment."):
            variable = path.rsplit(".", 1)[-1]
            return f"向依赖服务注入 {variable}；主机名和端口会替换为实际 Compose 服务信息。"
        if isinstance(value, dict):
            return f"{tech_stack_id} 模板中 `{path}` 的配置对象；子项必须结合项目逐一确认。"
        if isinstance(value, list):
            return f"{tech_stack_id} 模板中 `{path}` 的有序配置列表；调整时保留有效顺序。"
        return f"{tech_stack_id} 模板字段 `{path}`，当前建议值为 `{value}`；交付前按项目实际情况确认。"

    @classmethod
    def _default_comments(cls, tech_stack_id: str, json_data: dict[str, Any]) -> dict[str, str]:
        comments: dict[str, str] = {}

        def visit(value: Any, path: str = "$") -> None:
            comments[path] = cls._default_comment(tech_stack_id, path, value)
            if isinstance(value, dict):
                for key, child in value.items():
                    visit(child, f"{path}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{path}[{index}]")

        visit(json_data)
        return comments

    @classmethod
    def _merge_default_comments(cls, record: TechStack) -> dict[str, str]:
        comments = cls._default_comments(record.tech_stack_id, record.json_data)
        for path, comment in record.line_comments.items():
            if path in comments and str(comment).strip():
                comments[path] = str(comment)
        return comments

    def _record(self, payload: TechStackCreate, now: datetime | None = None) -> tuple[TechStack, dict[str, Any]]:
        normalized = self.catalog.validate_component(payload.tech_stack_id, payload.json_data)
        # Store the caller's YAML/JSON pair verbatim; the catalog receives the
        # normalized component so it can render reliable Compose documents.
        return TechStack(
            tech_stack_id=payload.tech_stack_id,
            name=payload.name,
            yaml_original=payload.yaml_original,
            json_data=deepcopy(payload.json_data),
            line_comments=self._comments(payload.json_data, payload.line_comments),
            created_at=now or utc_now(),
            updated_at=now or utc_now(),
        ), normalized

    def _synchronize_catalog(self) -> None:
        records = self.repository.list_tech_stacks()
        if not records:
            seed: list[TechStack] = []
            for name in self.catalog.names():
                component = deepcopy(self.catalog.components[name])
                yaml_original = self.catalog.component_yaml(name)
                seed.append(
                    TechStack(
                        tech_stack_id=name,
                        name=name,
                        yaml_original=yaml_original,
                        json_data=component,
                        line_comments=self._default_comments(name, component),
                    )
                )
            for record in seed:
                self.repository.create_tech_stack(record)
            records = seed
        synchronized: list[TechStack] = []
        for record in records:
            comments = self._merge_default_comments(record)
            if comments != record.line_comments:
                updated = self.repository.update_tech_stack(record.tech_stack_id, {"line_comments": comments})
                synchronized.append(updated or record.model_copy(update={"line_comments": comments}, deep=True))
            else:
                synchronized.append(record)
        records = synchronized
        self.catalog.load_components({record.tech_stack_id: record.json_data for record in records})

    def list(self) -> list[TechStack]:
        return self.repository.list_tech_stacks()

    def get(self, tech_stack_id: str) -> TechStack:
        record = self.repository.get_tech_stack(tech_stack_id)
        if record is None:
            raise NotFoundError("Technology stack not found")
        return record

    def create(self, payload: TechStackCreate) -> TechStack:
        record, normalized = self._record(payload)
        try:
            created = self.repository.create_tech_stack(record)
        except DuplicateTechStackError:
            raise
        self.catalog.upsert_component(payload.tech_stack_id, normalized)
        return created

    def update(self, tech_stack_id: str, payload: TechStackUpdate) -> TechStack:
        existing = self.get(tech_stack_id)
        fields = payload.model_dump(exclude_unset=True, by_alias=False)
        if fields.get("name") is None:
            fields.pop("name", None)
        yaml_original = fields.pop("yaml_original", None)
        json_data = fields.pop("json_data", None)
        comments = fields.pop("line_comments", None)
        if yaml_original is None and json_data is not None:
            yaml_original = yaml.safe_dump(json_data, allow_unicode=False, sort_keys=False)
        if json_data is None and yaml_original is not None:
            try:
                json_data = yaml.safe_load(yaml_original)
            except yaml.YAMLError as exc:
                raise TemplateError(f"yaml_original is invalid YAML: {exc}") from exc
        if yaml_original is not None or json_data is not None or comments is not None:
            yaml_original = yaml_original if yaml_original is not None else existing.yaml_original
            json_data = json_data if json_data is not None else existing.json_data
            candidate = TechStackCreate(
                tech_stack_id=tech_stack_id,
                name=str(fields.get("name", existing.name)),
                yaml_original=yaml_original,
                json_data=json_data,
                line_comments=comments if comments is not None else existing.line_comments,
            )
            _, normalized = self._record(candidate, now=existing.updated_at)
            fields.update(
                yaml_original=candidate.yaml_original,
                json_data=deepcopy(candidate.json_data),
                line_comments=candidate.line_comments,
            )
        if "name" not in fields and not (yaml_original is not None or json_data is not None or comments is not None):
            fields["name"] = existing.name
        fields["updated_at"] = utc_now()
        updated = self.repository.update_tech_stack(tech_stack_id, fields)
        if updated is None:
            raise NotFoundError("Technology stack not found")
        if yaml_original is not None or json_data is not None or comments is not None:
            self.catalog.upsert_component(tech_stack_id, normalized)
        if yaml_original is None and json_data is None and comments is None:
            self.catalog.upsert_component(tech_stack_id, self.catalog.components[tech_stack_id])
        return updated

    def delete(self, tech_stack_id: str) -> None:
        self.get(tech_stack_id)
        if not self.repository.delete_tech_stack(tech_stack_id):
            raise NotFoundError("Technology stack not found")
        self.catalog.remove_component(tech_stack_id)


class BuildService:
    def __init__(self, repository: Repository, application_service: ApplicationService, dispatcher: TaskDispatcher) -> None:
        self.repository = repository
        self.application_service = application_service
        self.dispatcher = dispatcher

    def _event(
        self, appid: str, build_id: str | None, kind: str, message: str, level: EventLevel = EventLevel.INFO, **details: Any
    ) -> None:
        self.repository.append_event(
            AppEvent(
                event_id=uuid4().hex,
                appid=appid,
                build_id=build_id,
                kind=kind,
                level=level,
                message=message,
                details=details,
            )
        )

    def record_event(
        self,
        appid: str,
        build_id: str | None,
        kind: str,
        message: str,
        level: EventLevel = EventLevel.INFO,
        **details: Any,
    ) -> None:
        """Persist a safe operational event from an asynchronous worker."""

        self._event(appid, build_id, kind, message, level, **details)

    def queue_build(self, appid: str, request: BuildCreate) -> BuildJob:
        app = self.application_service.get_record(appid)
        if not isinstance(app.compose, Mapping):
            raise BuildInputError(
                "A user-provided Docker Compose document with services is required before creating a build"
            )
        services = app.compose.get("services")
        if not isinstance(services, Mapping) or not services or not all(
            isinstance(name, str) and isinstance(service, Mapping) for name, service in services.items()
        ):
            raise BuildInputError("A user-provided Docker Compose document with services is required before creating a build")
        build = BuildJob(build_id=uuid4().hex, appid=appid, git_ref=request.git_ref or app.git_ref)
        self.repository.create_build(build)
        self._event(appid, build.build_id, "build.queued", "Build queued")
        try:
            submission = self.dispatcher.dispatch_build(build.build_id)
        except Exception as exc:
            self.fail(build.build_id, "Unable to enqueue build task")
            raise BuildInputError("Unable to enqueue build task") from exc
        updated = self.repository.update_build(build.build_id, {"celery_task_id": submission.task_id, "updated_at": utc_now()})
        if updated is None:
            raise NotFoundError("Build not found after queueing")
        self._event(appid, build.build_id, "build.dispatched", "Build task dispatched", task_id=submission.task_id)
        return updated

    def get_build(self, build_id: str) -> BuildJob:
        build = self.repository.get_build(build_id)
        if build is None:
            raise NotFoundError("Build not found")
        return build

    def transition(self, build_id: str, target: BuildStatus, **fields: Any) -> BuildJob:
        build = self.get_build(build_id)
        if build.status == target:
            return build
        if build.status in TERMINAL_BUILD_STATUSES or target not in ALLOWED_BUILD_TRANSITIONS[build.status]:
            raise InvalidBuildTransition(f"Cannot transition build from {build.status.value} to {target.value}")
        now = utc_now()
        fields = {**fields, "updated_at": now}
        if target == BuildStatus.VALIDATING and build.started_at is None:
            fields["started_at"] = now
        if target in TERMINAL_BUILD_STATUSES:
            fields["finished_at"] = now
        updated = self.repository.transition_build(build_id, [build.status], target, fields)
        if updated is None:
            current = self.get_build(build_id)
            if current.status == target:
                return current
            raise InvalidBuildTransition("Concurrent build status update")
        level = EventLevel.ERROR if target == BuildStatus.FAILED else EventLevel.INFO
        self._event(updated.appid, build_id, f"build.{target.value}", f"Build is {target.value}", level)
        return updated

    def fail(self, build_id: str, message: str) -> BuildJob:
        build = self.get_build(build_id)
        if build.status == BuildStatus.FAILED:
            return build
        if build.status in TERMINAL_BUILD_STATUSES:
            return build
        failed = self.transition(build_id, BuildStatus.FAILED, error=message)
        self.repository.create_alert(
            Alert(
                alert_id=uuid4().hex,
                appid=failed.appid,
                build_id=failed.build_id,
                severity="error",
                message=message,
            )
        )
        self._event(failed.appid, failed.build_id, "build.alert", message, EventLevel.ERROR)
        return failed
