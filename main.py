from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from orchestrator.config import Settings, get_settings

app = FastAPI(title="Docker-Jenkins Orchestrator", version="0.1.0")
bearer = HTTPBearer(auto_error=False)


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
