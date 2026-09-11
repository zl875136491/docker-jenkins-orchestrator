from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel
from uuid import uuid4
from pathlib import Path

from orchestrator.config import Settings, get_settings
from orchestrator.models import BuildCreate, BuildJob, UserApp, UserAppCreate
from orchestrator.repository import DuplicateAppError, InMemoryRepository
from orchestrator.tasks import InMemoryTaskDispatcher
from orchestrator.templates import TemplateCatalog, TemplateError

app = FastAPI(title="Docker-Jenkins Orchestrator", version="0.1.0")
bearer = HTTPBearer(auto_error=False)
repository = InMemoryRepository()
dispatcher = InMemoryTaskDispatcher()
catalog = TemplateCatalog(Path(__file__).parent / "templates" / "catalog.yaml")


class ConnectRequest(BaseModel):
    worker_name: str
    worker_secret: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


def issue_token(settings: Settings) -> TokenResponse:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": settings.worker_name, "scope": "conductor", "iat": now, "exp": expires}
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return TokenResponse(access_token=token, expires_at=expires)


def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> dict:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        return jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/connect", response_model=TokenResponse)
def connect(request: ConnectRequest, settings: Settings = Depends(get_settings)) -> TokenResponse:
    if request.worker_name != settings.worker_name or request.worker_secret != settings.worker_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker credentials")
    return issue_token(settings)


@app.get("/api/me")
def me(claims: dict = Depends(require_token)) -> dict[str, str]:
    return {"worker_name": str(claims["sub"]), "scope": str(claims.get("scope", ""))}


@app.post("/api/apps", response_model=UserApp, status_code=status.HTTP_201_CREATED)
def create_app(request: UserAppCreate, claims: dict = Depends(require_token)) -> UserApp:
    del claims
    app_record = UserApp(**request.model_dump())
    try:
        return repository.create_app(app_record)
    except DuplicateAppError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="appid already exists") from exc


@app.get("/api/apps/{appid}", response_model=UserApp)
def get_app(appid: str, claims: dict = Depends(require_token)) -> UserApp:
    del claims
    app_record = repository.get_app(appid)
    if app_record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
    return app_record


@app.post("/api/apps/{appid}/builds", response_model=BuildJob, status_code=status.HTTP_202_ACCEPTED)
def create_build(appid: str, request: BuildCreate, claims: dict = Depends(require_token)) -> BuildJob:
    del claims
    if repository.get_app(appid) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
    build = BuildJob(build_id=uuid4().hex, appid=appid, git_ref=request.git_ref)
    repository.create_build(build)
    dispatcher.dispatch_build(build)
    return build


@app.get("/api/builds/{build_id}", response_model=BuildJob)
def get_build(build_id: str, claims: dict = Depends(require_token)) -> BuildJob:
    del claims
    build = repository.get_build(build_id)
    if build is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build not found")
    return build


@app.get("/api/templates")
def list_templates(claims: dict = Depends(require_token)) -> dict[str, list[str]]:
    del claims
    return {"components": catalog.names()}


class ComposeRequest(BaseModel):
    components: list[str]
    dependencies: dict[str, list[str]] = {}


@app.post("/api/templates/compose")
def compose_template(request: ComposeRequest, claims: dict = Depends(require_token)) -> dict:
    del claims
    try:
        return catalog.compose(request.components, request.dependencies)
    except TemplateError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
