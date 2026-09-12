from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from orchestrator.config import Settings, get_settings
from orchestrator.container import ApplicationContainer, create_container
from orchestrator.models import BuildCreate, BuildJob, UserApp, UserAppCreate, UserAppUpdate
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

    @api.post("/api/apps", response_model=UserApp, status_code=status.HTTP_201_CREATED)
    def create_user_app(request: UserAppCreate, _: dict = Depends(require_token)) -> UserApp:
        try:
            return container.applications.create_app(request)
        except DuplicateAppError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="appid already exists") from exc

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

    return api


app = create_app()
