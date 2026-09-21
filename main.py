from __future__ import annotations

import hmac
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from orchestrator.config import Settings, get_settings
from orchestrator.container import ApplicationContainer, create_container
from orchestrator.models import (
    AppAccess,
    BuildCreate,
    BuildHistoryPage,
    BuildJob,
    BuildStatus,
    PublishedPort,
    ServiceAccess,
    UserApp,
    UserAppCreate,
    UserAppUpdate,
)
from orchestrator.repository import DuplicateAppError
from orchestrator.services import BuildInputError, NotFoundError
from orchestrator.templates import TemplateError

bearer = HTTPBearer(auto_error=False)


class ConnectRequest(BaseModel):
    worker_name: str
    worker_secret: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class ComposeRequest(BaseModel):
    components: list[str]
    dependencies: dict[str, list[str]] = Field(default_factory=dict)


_LEGACY_ENDPOINT_PORT = re.compile(r":(?P<port>[1-9]\d{0,4})$")


def _legacy_published_ports(endpoint: str | None) -> list[PublishedPort]:
    """Recover the old single-port endpoint representation when available."""

    if not endpoint:
        return []
    match = _LEGACY_ENDPOINT_PORT.search(endpoint.strip())
    if match is None:
        return []
    port = int(match.group("port"))
    if port > 65535:
        return []
    return [PublishedPort(target_port=port, published_port=port)]


def _public_host(host: str | None) -> str | None:
    if not isinstance(host, str):
        return None
    value = host.strip().rstrip("/")
    if not value or "://" in value:
        return None
    if value.count(":") > 1 and not value.startswith("["):
        return f"[{value}]"
    return value


def _access_urls(settings: Settings, ports: list[PublishedPort]) -> list[str]:
    host = _public_host(settings.public_host)
    if host is None:
        return []
    urls: list[str] = []
    for port in ports:
        if port.published_port is None or port.protocol != "tcp":
            continue
        url = f"{settings.public_scheme}://{host}:{port.published_port}"
        if url not in urls:
            urls.append(url)
    return urls


def _access_reason(settings: Settings, ports: list[PublishedPort], urls: list[str]) -> str | None:
    if _public_host(settings.public_host) is None:
        return "Public host is not configured"
    if not ports or not any(port.published_port is not None for port in ports):
        return "No published ports"
    if not urls:
        return "No TCP published ports"
    return None


def get_container(request: Request) -> ApplicationContainer:
    return request.app.state.container


def issue_token(settings: Settings) -> TokenResponse:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": settings.worker_name, "scope": "conductor", "iat": now, "exp": expires}
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return TokenResponse(access_token=token, expires_at=expires)


def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    container: ApplicationContainer = Depends(get_container),
) -> dict:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        claims = jwt.decode(credentials.credentials, container.settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    if claims.get("scope") != "conductor":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token scope")
    return claims


def create_app(
    settings: Settings | None = None,
    container: ApplicationContainer | None = None,
) -> FastAPI:
    if container is None:
        container = create_container(settings or get_settings())

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        container.close()

    api = FastAPI(title="Docker-Jenkins Orchestrator", version="0.2.0", lifespan=lifespan)
    api.state.container = container
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    @api.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.post("/api/connect", response_model=TokenResponse)
    def connect(request: ConnectRequest) -> TokenResponse:
        settings = container.settings
        name_matches = hmac.compare_digest(request.worker_name, settings.worker_name)
        secret_matches = hmac.compare_digest(request.worker_secret, settings.worker_secret)
        if not name_matches or not secret_matches:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker credentials")
        return issue_token(settings)

    @api.get("/api/me")
    def me(claims: dict = Depends(require_token)) -> dict[str, str]:
        return {"worker_name": str(claims["sub"]), "scope": str(claims["scope"])}

    @api.get("/api/system-guide")
    def system_guide(_: dict = Depends(require_token)) -> dict:
        """Return the machine-readable control-plane workflow and conventions."""
        return {
            "name": "Docker-Jenkins Orchestrator",
            "version": "0.2.0",
            "authentication": {"connect": "POST /api/connect", "scheme": "Bearer JWT"},
            "roles": ["webui", "control_api", "mongodb", "redis_celery", "worker", "jenkins", "harbor", "docker_swarm"],
            "call_sequence": [
                "POST /api/connect", "GET /api/apps", "POST /api/apps or PATCH /api/apps/{appid}",
                "POST /api/apps/{appid}/builds", "GET /api/builds/{build_id}",
                "GET /api/apps/{appid}/events", "GET /api/apps/{appid}/images",
                "GET /api/apps/{appid}/services", "GET /api/apps/{appid}/access", "GET /api/apps/{appid}/alerts",
            ],
            "build_statuses": [status.value for status in BuildStatus],
            "compose_rules": {
                "required_before_build": True,
                "git_auto_discovery": False,
                "external_access_requires_ports": True,
                "expose_is_external": False,
            },
            "polling": {
                "endpoint": "GET /api/builds/{build_id}",
                "terminal_statuses": [BuildStatus.SUCCEEDED.value, BuildStatus.FAILED.value, BuildStatus.CANCELLED.value],
            },
            "troubleshooting_order": ["webui_request", "control_api", "mongo", "celery_redis", "jenkins", "harbor", "docker_swarm", "external_http"],
        }

    @api.get("/api/readme", response_class=PlainTextResponse)
    def readme(_: dict = Depends(require_token)) -> str:
        """Return the complete Markdown API usage guide."""
        guide_path = Path(__file__).parent / "docs" / "api-reference.md"
        flow_path = Path(__file__).parent / "docs" / "api-call-flow.md"
        return f"{guide_path.read_text(encoding='utf-8').rstrip()}\n\n---\n\n{flow_path.read_text(encoding='utf-8').rstrip()}\n"

    @api.post("/api/apps", response_model=UserApp, status_code=status.HTTP_201_CREATED)
    def create_user_app(request: UserAppCreate, _: dict = Depends(require_token)) -> UserApp:
        try:
            return container.applications.create_app(request)
        except DuplicateAppError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="appid already exists") from exc

    @api.get("/api/apps", response_model=list[UserApp])
    def list_user_apps(_: dict = Depends(require_token)) -> list[UserApp]:
        return container.applications.list_apps()

    @api.get("/api/apps/{appid}", response_model=UserApp)
    def get_user_app(appid: str, _: dict = Depends(require_token)) -> UserApp:
        try:
            return container.applications.get_app(appid)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc

    @api.patch("/api/apps/{appid}", response_model=UserApp)
    def update_user_app(appid: str, request: UserAppUpdate, _: dict = Depends(require_token)) -> UserApp:
        try:
            return container.applications.update_app(appid, request)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc

    @api.post("/api/apps/{appid}/builds", response_model=BuildJob, status_code=status.HTTP_202_ACCEPTED)
    def create_build(appid: str, request: BuildCreate, _: dict = Depends(require_token)) -> BuildJob:
        try:
            return container.builds.queue_build(appid, request)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc
        except BuildInputError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    @api.get("/api/builds/{build_id}", response_model=BuildJob)
    def get_build(build_id: str, _: dict = Depends(require_token)) -> BuildJob:
        try:
            return container.builds.get_build(build_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build not found") from exc

    @api.get("/api/builds", response_model=BuildHistoryPage)
    def list_builds(
        appid: str | None = Query(default=None, min_length=1, max_length=64),
        build_status: BuildStatus | None = Query(default=None, alias="status"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        _: dict = Depends(require_token),
    ) -> BuildHistoryPage:
        """List persisted build history for the control console and conductor."""

        items, total = container.repository.list_builds(
            appid=appid,
            status=build_status,
            skip=(page - 1) * page_size,
            limit=page_size,
        )
        return BuildHistoryPage(items=items, total=total, page=page, page_size=page_size)

    @api.get("/api/apps/{appid}/events")
    def list_events(appid: str, _: dict = Depends(require_token)) -> list:
        try:
            container.applications.get_record(appid)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc
        return container.repository.list_events(appid)

    @api.get("/api/apps/{appid}/images")
    def list_images(appid: str, _: dict = Depends(require_token)) -> list:
        try:
            container.applications.get_record(appid)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc
        return container.repository.list_user_images(appid)

    @api.get("/api/apps/{appid}/services")
    def list_services(appid: str, _: dict = Depends(require_token)) -> list:
        try:
            container.applications.get_record(appid)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc
        return container.repository.list_services(appid)

    @api.get("/api/apps/{appid}/access", response_model=AppAccess)
    def get_app_access(appid: str, _: dict = Depends(require_token)) -> AppAccess:
        try:
            container.applications.get_record(appid)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc

        service_access: list[ServiceAccess] = []
        for service in container.repository.list_services(appid):
            ports = list(service.published_ports) or _legacy_published_ports(service.endpoint)
            urls = _access_urls(container.settings, ports)
            service_access.append(
                ServiceAccess(
                    build_id=service.build_id,
                    service_name=service.service_name,
                    status=service.status,
                    image=service.image,
                    endpoint=service.endpoint,
                    published_ports=ports,
                    access_urls=urls,
                    access_available=bool(urls),
                    access_reason=_access_reason(container.settings, ports, urls),
                )
            )

        access_urls: list[str] = []
        for service in service_access:
            for url in service.access_urls:
                if url not in access_urls:
                    access_urls.append(url)
        app_reason = None
        if not service_access:
            app_reason = "当前应用没有已部署服务"
        elif not access_urls:
            app_reason = "服务没有配置可从外部访问的 TCP published 端口"
        return AppAccess(
            appid=appid,
            services=service_access,
            access_available=bool(access_urls),
            access_urls=access_urls,
            access_reason=app_reason,
        )

    @api.get("/api/apps/{appid}/alerts")
    def list_alerts(appid: str, _: dict = Depends(require_token)) -> list:
        try:
            container.applications.get_record(appid)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc
        return container.repository.list_alerts(appid)

    @api.get("/api/templates")
    def list_templates(_: dict = Depends(require_token)) -> dict[str, list[str]]:
        return {"components": container.catalog.names()}

    @api.post("/api/templates/compose")
    def compose_template(request: ComposeRequest, _: dict = Depends(require_token)) -> dict:
        try:
            return container.catalog.compose(request.components, request.dependencies)
        except TemplateError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    @api.get("/api/base-images")
    def list_base_images(_: dict = Depends(require_token)) -> list:
        return container.repository.list_base_images()

    @api.post("/api/base-images/sync", status_code=status.HTTP_202_ACCEPTED)
    def sync_base_images(_: dict = Depends(require_token)) -> dict[str, str]:
        submission = container.dispatcher.dispatch_base_image_sync()
        return {"task_id": submission.task_id, "task_name": submission.task_name}

    # Keep the control surface dependency-free: the API container serves the
    # small static console alongside the JSON API.
    @api.get("/", include_in_schema=False)
    def control_console_redirect() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    api.mount(
        "/ui",
        StaticFiles(directory=Path(__file__).parent / "webui", html=True),
        name="webui",
    )

    return api


app = create_app()
