from datetime import datetime, timedelta, timezone
import json
from typing import get_args

from fastapi.testclient import TestClient
from jose import jwt
import pytest

from main import create_app, issue_tokens
from orchestrator.config import Settings
from orchestrator.models import (
    Alert,
    AppEvent,
    AppSyncResult,
    BaseImage,
    BuildHistoryPage,
    BuildJob,
    DeploymentService,
    TechStack,
    UserApp,
    UserAppRecord,
    UserImage,
    utc_now,
)
from orchestrator.repository import MongoRepository


UTC_TIMESTAMP = datetime(2026, 10, 14, 1, 55, 20, 795000, tzinfo=timezone.utc)
EXPECTED_TIMESTAMP = "2026-10-14T09:55:20.795000+08:00"


def timestamp_fields(model):
    return [
        name for name, field in type(model).model_fields.items()
        if field.annotation is datetime or datetime in get_args(field.annotation)
    ]


def platform_records(timestamp):
    app = UserAppRecord(appid="timezone-app", name="Timezone app", repository_url="", git_ref="main", environment_ciphertext="")
    records = [
        app,
        UserApp.from_record(app),
        AppSyncResult(appid=app.appid, git_ref="main", commit_id="a" * 40, compose_file="compose.yaml",
                      compose_changed=True, commit_changed=True, compose={"services": {}}, updated_at=timestamp),
        BuildJob(build_id="timezone-build", appid=app.appid, git_ref="main"),
        AppEvent(event_id="timezone-event", appid=app.appid, kind="test", message="Timezone event"),
        UserImage(image_id="timezone-image", appid=app.appid, build_id="timezone-build", reference="example/web:1"),
        BaseImage(source_image="nginx:alpine", harbor_reference="registry.example/nginx:alpine"),
        TechStack(tech_stack_id="timezone-stack", name="Timezone stack", yaml_original="image: nginx",
                  json_data={"image": "nginx"}),
        DeploymentService(service_id="timezone-service", appid=app.appid, build_id="timezone-build",
                          service_name="web", image="nginx:alpine", status="running"),
        Alert(alert_id="timezone-alert", appid=app.appid, severity="error", message="Timezone alert"),
    ]
    return [record.model_copy(update={field: timestamp for field in timestamp_fields(record)}) for record in records]


@pytest.mark.parametrize("timestamp", [
    UTC_TIMESTAMP,
    UTC_TIMESTAMP.replace(tzinfo=None),
    UTC_TIMESTAMP.astimezone(timezone(timedelta(hours=8))),
    UTC_TIMESTAMP.astimezone(timezone(timedelta(hours=-5))),
], ids=["utc", "mongo-naive-utc", "already-east-eight", "other-offset"])
def test_all_platform_timestamp_fields_use_east_eight_without_changing_python_storage(timestamp):
    for record in platform_records(timestamp):
        payload = record.model_dump(mode="json")
        encoded = json.loads(record.model_dump_json())
        document = MongoRepository._document(record)
        for field in timestamp_fields(record):
            assert payload[field] == EXPECTED_TIMESTAMP
            assert encoded[field] == EXPECTED_TIMESTAMP
            assert datetime.fromisoformat(payload[field]).astimezone(timezone.utc) == UTC_TIMESTAMP
            assert isinstance(document[field], datetime)
            assert document[field].hour == timestamp.hour
            assert document[field].tzinfo == timestamp.tzinfo
            assert getattr(record, field) == timestamp


def test_json_timestamp_conversion_crosses_day_boundary_and_preserves_nulls():
    build = BuildJob(build_id="night-build", appid="timezone-app", git_ref="main",
                     created_at=datetime(2026, 10, 13, 20, 30, tzinfo=timezone.utc))
    page = BuildHistoryPage(items=[build], total=1, page=1, page_size=20)
    payload = page.model_dump(mode="json")["items"][0]
    assert payload["created_at"] == "2026-10-14T04:30:00+08:00"
    assert payload["started_at"] is None
    assert payload["finished_at"] is None
    assert payload["last_polled_at"] is None
    assert page.model_dump()["items"][0]["created_at"].hour == 20


def test_jwt_and_internal_clock_remain_in_utc():
    settings = Settings()
    before = datetime.now(timezone.utc).timestamp()
    tokens = issue_tokens(settings)
    after = datetime.now(timezone.utc).timestamp()
    for token, lifetime in ((tokens.access_token, settings.jwt_expire_minutes * 60),
                            (tokens.refresh_token, settings.jwt_refresh_expire_days * 24 * 60 * 60)):
        claims = jwt.get_unverified_claims(token)
        assert isinstance(claims["iat"], int)
        assert isinstance(claims["exp"], int)
        assert before - 1 <= claims["iat"] <= after
        assert claims["exp"] - claims["iat"] == lifetime
    assert utc_now().utcoffset() == timedelta(0)
    assert tokens.expires_in == settings.jwt_expire_minutes * 60


@pytest.mark.parametrize("timestamp", [UTC_TIMESTAMP, UTC_TIMESTAMP.replace(tzinfo=None)], ids=["aware-utc", "historical-naive"])
def test_api_timestamps_cover_typed_nested_and_untyped_resource_responses(tmp_path, timestamp):
    settings = Settings(workspace_root=tmp_path / "workspaces")
    with TestClient(create_app(settings)) as client:
        token = client.post("/oauth2/token", json={"client_id": settings.worker_name, "client_secret": settings.worker_secret})
        assert token.status_code == 200
        client.headers["Authorization"] = "Bearer " + token.json()["access_token"]
        repository = client.app.state.container.repository
        records = platform_records(timestamp)
        by_type = {type(record): record for record in records}
        repository.create_app(by_type[UserAppRecord])
        repository.create_build(by_type[BuildJob])
        repository.append_event(by_type[AppEvent])
        repository.save_user_image(by_type[UserImage])
        repository.save_base_image(by_type[BaseImage])
        repository.create_tech_stack(by_type[TechStack])
        repository.save_service(by_type[DeploymentService])
        repository.create_alert(by_type[Alert])
        endpoints = [
            "/api/v1/jenkins/app_info/timezone-app",
            "/api/v1/jenkins/app_list",
            "/api/v1/jenkins/build_info/timezone-build",
            "/api/v1/jenkins/build_list?app_id=timezone-app",
            "/api/v1/jenkins/app_events/timezone-app",
            "/api/v1/jenkins/app_images/timezone-app",
            "/api/v1/jenkins/app_services/timezone-app",
            "/api/v1/jenkins/app_alerts/timezone-app",
            "/api/v1/docker/base_image_list",
            "/api/v1/docker/tech_stack_info/timezone-stack",
            "/api/v1/docker/tech_stack_list",
        ]
        for endpoint in endpoints:
            response = client.get(endpoint)
            assert response.status_code == 200, response.text
            payload = response.json()
            items = payload if isinstance(payload, list) else payload.get("items", [payload])
            assert items
            for item in items:
                dates = {key: value for key, value in item.items() if key.endswith("_at")}
                assert dates
                for value in dates.values():
                    if value is not None:
                        assert value.endswith("+08:00")
                        assert datetime.fromisoformat(value).utcoffset() == timedelta(hours=8)
            if "tech_stack_list" not in endpoint:
                assert items[0].get("created_at", items[0].get("updated_at")) == EXPECTED_TIMESTAMP
        guide = client.get("/api/v1/system-guide").json()
        assert guide["timestamps"] == {"timezone": "Asia/Shanghai", "utc_offset": "+08:00", "format": "ISO 8601", "storage": "UTC"}
        assert repository.get_app("timezone-app").created_at.tzinfo == timestamp.tzinfo
