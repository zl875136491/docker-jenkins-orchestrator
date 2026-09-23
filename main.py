from __future__ import annotations

import hmac
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
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
    TechStack,
    TechStackCreate,
    TechStackUpdate,
    UserApp,
    UserAppCreate,
    UserAppUpdate,
)
from orchestrator.repository import DuplicateAppError, DuplicateTechStackError
from orchestrator.services import BuildInputError, NotFoundError
from orchestrator.templates import TemplateError

bearer = HTTPBearer(auto_error=False)


class TokenRequest(BaseModel):
    client_id: str
    client_secret: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class ComposeRequest(BaseModel):
    components: list[str]
    dependencies: dict[str, list[str] | dict[str, str]] = Field(default_factory=dict)


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


def issue_tokens(settings: Settings, subject: str | None = None) -> TokenResponse:
    now = datetime.now(timezone.utc)
    access_expires = now + timedelta(minutes=settings.jwt_expire_minutes)
    refresh_expires = now + timedelta(days=settings.jwt_refresh_expire_days)
    principal = subject or settings.worker_name
    common = {"sub": principal, "scope": "conductor", "iat": now}
    access_payload = {**common, "token_type": "access", "exp": access_expires}
    refresh_payload = {**common, "token_type": "refresh", "exp": refresh_expires}
    return TokenResponse(
        access_token=jwt.encode(access_payload, settings.jwt_secret, algorithm="HS256"),
        refresh_token=jwt.encode(refresh_payload, settings.jwt_secret, algorithm="HS256"),
        expires_in=max(1, int((access_expires - now).total_seconds())),
    )


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
    if claims.get("scope") != "conductor" or claims.get("token_type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token scope")
    return claims


def decode_refresh_token(token: str, settings: Settings) -> dict:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token") from exc
    if claims.get("scope") != "conductor" or claims.get("token_type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
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

    @api.post("/oauth2/token", response_model=TokenResponse)
    def token(request: TokenRequest) -> TokenResponse:
        settings = container.settings
        name_matches = hmac.compare_digest(request.client_id, settings.worker_name)
        secret_matches = hmac.compare_digest(request.client_secret, settings.worker_secret)
        if not name_matches or not secret_matches:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid client credentials")
        return issue_tokens(settings, request.client_id)

    @api.post("/oauth2/refresh", response_model=TokenResponse)
    def refresh(request: RefreshRequest) -> TokenResponse:
        claims = decode_refresh_token(request.refresh_token, container.settings)
        return issue_tokens(container.settings, str(claims["sub"]))

    @api.get("/api/v1/system-guide")
    def system_guide(_: dict = Depends(require_token)) -> dict:
        """Return the machine-readable control-plane workflow and conventions."""
        return {
            "name": "Docker-Jenkins Orchestrator",
            "version": "0.2.0",
            "authentication": {"token": "POST /oauth2/token", "refresh": "POST /oauth2/refresh", "scheme": "Bearer JWT"},
            "roles": ["webui", "control_api", "mongodb", "redis_celery", "worker", "jenkins", "harbor", "docker_swarm"],
            "call_sequence": [
                "POST /oauth2/token", "GET /api/v1/jenkins/app_list", "POST /api/v1/jenkins/app_create or PATCH /api/v1/jenkins/app_info/{app_id}",
                "POST /api/v1/jenkins/build_create/{app_id}", "GET /api/v1/jenkins/build_info/{build_id}",
                "GET /api/v1/jenkins/app_events/{app_id}", "GET /api/v1/jenkins/app_images/{app_id}",
                "GET /api/v1/jenkins/app_services/{app_id}", "GET /api/v1/jenkins/app_access/{app_id}", "GET /api/v1/jenkins/app_alerts/{app_id}",
                "GET/POST/PATCH/DELETE /api/v1/docker/tech_stack_*", "GET /api/v1/docker/compose_prompt",
            ],
            "build_statuses": [status.value for status in BuildStatus],
            "compose_rules": {
                "required_before_build": True,
                "git_auto_discovery": False,
                "external_access_requires_ports": True,
                "expose_is_external": False,
            },
            "tech_stack_schema": {
                "yaml_original": "original YAML text",
                "json_data": "parsed JSON object; must equal yaml_original after parsing",
                "line_comments": "JSONPath-to-comment mapping covering every JSON path; missing values are empty strings",
            },
            "polling": {
                "endpoint": "GET /api/v1/jenkins/build_info/{build_id}",
                "terminal_statuses": [BuildStatus.SUCCEEDED.value, BuildStatus.FAILED.value, BuildStatus.CANCELLED.value],
            },
            "troubleshooting_order": ["webui_request", "control_api", "mongo", "celery_redis", "jenkins", "harbor", "docker_swarm", "external_http"],
        }

    @api.get("/api/v1/readme", response_class=PlainTextResponse)
    def readme(_: dict = Depends(require_token)) -> str:
        """Return the complete Markdown API usage guide."""
        guide_path = Path(__file__).parent / "docs" / "api-reference.md"
        flow_path = Path(__file__).parent / "docs" / "api-call-flow.md"
        return f"{guide_path.read_text(encoding='utf-8').rstrip()}\n\n---\n\n{flow_path.read_text(encoding='utf-8').rstrip()}\n"

    @api.get("/api/v1/docker/compose_prompt", response_class=PlainTextResponse)
    def compose_prompt(_: dict = Depends(require_token)) -> PlainTextResponse:
        """Return a Markdown prompt preloaded with the current stack templates."""

        prompt_path = Path(__file__).parent / "docs" / "compose-generation-prompt.md"
        prompt = prompt_path.read_text(encoding="utf-8").rstrip()
        stacks = container.tech_stacks.list()
        catalog_text = ["", "## 当前可用技术栈模板", ""]
        for stack in stacks:
            catalog_text.extend(
                [
                    f"### {stack.tech_stack_id} ({stack.name})",
                    "```yaml",
                    stack.yaml_original.rstrip(),
                    "```",
                    "",
                ]
            )
        return PlainTextResponse("\n".join([prompt, *catalog_text]), media_type="text/markdown")

    @api.post("/api/v1/jenkins/app_create", response_model=UserApp, status_code=status.HTTP_201_CREATED)
    def create_user_app(request: UserAppCreate, _: dict = Depends(require_token)) -> UserApp:
        try:
            return container.applications.create_app(request)
        except DuplicateAppError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="appid already exists") from exc

    @api.get("/api/v1/jenkins/app_list", response_model=list[UserApp])
    def list_user_apps(_: dict = Depends(require_token)) -> list[UserApp]:
        return container.applications.list_apps()

    @api.get("/api/v1/jenkins/app_info/{app_id}", response_model=UserApp)
    def get_user_app(app_id: str, _: dict = Depends(require_token)) -> UserApp:
        try:
            return container.applications.get_app(app_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc

    @api.patch("/api/v1/jenkins/app_info/{app_id}", response_model=UserApp)
    def update_user_app(app_id: str, request: UserAppUpdate, _: dict = Depends(require_token)) -> UserApp:
        try:
            return container.applications.update_app(app_id, request)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc

    @api.post("/api/v1/jenkins/build_create/{app_id}", response_model=BuildJob, status_code=status.HTTP_202_ACCEPTED)
    def create_build(app_id: str, request: BuildCreate, _: dict = Depends(require_token)) -> BuildJob:
        try:
            return container.builds.queue_build(app_id, request)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc
        except BuildInputError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    @api.get("/api/v1/jenkins/build_info/{build_id}", response_model=BuildJob)
    def get_build(build_id: str, _: dict = Depends(require_token)) -> BuildJob:
        try:
            return container.builds.get_build(build_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build not found") from exc

    @api.get("/api/v1/jenkins/build_list", response_model=BuildHistoryPage)
    def list_builds(
        app_id: str | None = Query(default=None, min_length=1, max_length=64),
        build_status: BuildStatus | None = Query(default=None, alias="status"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        _: dict = Depends(require_token),
    ) -> BuildHistoryPage:
        """List persisted build history for the control console and conductor."""

        items, total = container.repository.list_builds(
            appid=app_id,
            status=build_status,
            skip=(page - 1) * page_size,
            limit=page_size,
        )
        return BuildHistoryPage(items=items, total=total, page=page, page_size=page_size)

    @api.get("/api/v1/jenkins/app_events/{app_id}")
    def list_events(app_id: str, _: dict = Depends(require_token)) -> list:
        try:
            container.applications.get_record(app_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc
        return container.repository.list_events(app_id)

    @api.get("/api/v1/jenkins/app_images/{app_id}")
    def list_images(app_id: str, _: dict = Depends(require_token)) -> list:
        try:
            container.applications.get_record(app_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc
        return container.repository.list_user_images(app_id)

    @api.get("/api/v1/jenkins/app_services/{app_id}")
    def list_services(app_id: str, _: dict = Depends(require_token)) -> list:
        try:
            container.applications.get_record(app_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc
        return container.repository.list_services(app_id)

    @api.get("/api/v1/jenkins/app_access/{app_id}", response_model=AppAccess)
    def get_app_access(app_id: str, _: dict = Depends(require_token)) -> AppAccess:
        try:
            container.applications.get_record(app_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc

        service_access: list[ServiceAccess] = []
        for service in container.repository.list_services(app_id):
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
            appid=app_id,
            services=service_access,
            access_available=bool(access_urls),
            access_urls=access_urls,
            access_reason=app_reason,
        )

    @api.get("/api/v1/jenkins/app_alerts/{app_id}")
    def list_alerts(app_id: str, _: dict = Depends(require_token)) -> list:
        try:
            container.applications.get_record(app_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found") from exc
        return container.repository.list_alerts(app_id)

    @api.get("/api/v1/docker/tech_stack_list", response_model=list[TechStack])
    def list_tech_stacks(_: dict = Depends(require_token)) -> list[TechStack]:
        return container.tech_stacks.list()

    @api.post("/api/v1/docker/tech_stack_create", response_model=TechStack, status_code=status.HTTP_201_CREATED)
    def create_tech_stack(request: TechStackCreate, _: dict = Depends(require_token)) -> TechStack:
        try:
            return container.tech_stacks.create(request)
        except DuplicateTechStackError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="tech_stack_id already exists") from exc
        except (TemplateError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    @api.get("/api/v1/docker/tech_stack_info/{tech_stack_id}", response_model=TechStack)
    def get_tech_stack(tech_stack_id: str, _: dict = Depends(require_token)) -> TechStack:
        try:
            return container.tech_stacks.get(tech_stack_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Technology stack not found") from exc

    @api.patch("/api/v1/docker/tech_stack_info/{tech_stack_id}", response_model=TechStack)
    def update_tech_stack(tech_stack_id: str, request: TechStackUpdate, _: dict = Depends(require_token)) -> TechStack:
        try:
            return container.tech_stacks.update(tech_stack_id, request)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Technology stack not found") from exc
        except (TemplateError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    @api.delete("/api/v1/docker/tech_stack_info/{tech_stack_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_tech_stack(tech_stack_id: str, _: dict = Depends(require_token)) -> Response:
        try:
            container.tech_stacks.delete(tech_stack_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Technology stack not found") from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get("/api/v1/docker/template_list")
    def list_templates(_: dict = Depends(require_token)) -> dict:
        return {"components": container.catalog.names(), "tech_stacks": container.tech_stacks.list()}

    @api.post("/api/v1/docker/template_compose")
    def compose_template(request: ComposeRequest, _: dict = Depends(require_token)) -> dict:
        try:
            return container.catalog.compose(request.components, request.dependencies)
        except TemplateError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    @api.get("/api/v1/docker/base_image_list")
    def list_base_images(_: dict = Depends(require_token)) -> list:
        return container.repository.list_base_images()

    @api.post("/api/v1/docker/base_image_sync", status_code=status.HTTP_202_ACCEPTED)
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
