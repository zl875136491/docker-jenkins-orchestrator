#!/usr/bin/env python3
"""Import recent, non-secret app configurations and test an isolated target.

The source is only read through its public API. Credentials stay in process
memory. No GitLab/Jenkins/registry secrets, old build states, service IDs, or
workspace contents are copied. Writes are restricted to TEST110-* apps and a
small temporary workspace marker. Builds/checks are only submitted after their
missing prerequisites have been confirmed, to verify rejection before dispatch.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import textwrap
from urllib.parse import quote, urlsplit

import requests


ROOT = Path(__file__).resolve().parents[1]
BUILD_GATE_ERROR = "未进行路由静态检查, 无法交付构建任务"
GITLAB_ERROR = "Route checks require a configured GitLab repository adapter"


def remote_json(host: str, container: str, code: str) -> dict:
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
         "docker exec -i " + shlex.quote(container) + " python -"],
        input=textwrap.dedent(code), capture_output=True, text=True,
        timeout=90, check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"Read-only remote query failed for {host}; output withheld")
    return json.loads(completed.stdout)


SOURCE_EXPORT = '''
import json, os
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

def get(path, token=None, body=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = Request('http://127.0.0.1:8000' + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.load(response)

token = get('/oauth2/token', body={
    'client_id': os.environ['ORCHESTRATOR_WORKER_NAME'],
    'client_secret': os.environ['ORCHESTRATOR_WORKER_SECRET'],
})['access_token']
apps = get('/api/v1/jenkins/app_list', token)
def timestamp(app):
    raw = app.get('updated_at') or app.get('created_at')
    value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
apps.sort(key=timestamp, reverse=True)
selected = apps[:SAMPLE_COUNT]
metadata = []
for app in selected:
    appid = app['appid']
    history = get('/api/v1/jenkins/build_list?' + urlencode({
        'app_id': appid, 'page': 1, 'page_size': 1}), token)
    workspace = get('/api/v1/workspace/' + quote(appid, safe=''), token)
    metadata.append({
        'appid': appid, 'build_total': history['total'],
        'latest_build': [{key: build.get(key) for key in (
            'build_id', 'status', 'created_at', 'git_commit_sha')}
            for build in history.get('items', [])],
        'workspace_file_count': len(workspace.get('files', [])),
    })
print(json.dumps({'total_apps': len(apps), 'apps': selected, 'metadata': metadata}))
'''

TARGET_CONFIG = '''
import json
from urllib.parse import urlsplit
from orchestrator.config import get_settings
s = get_settings()
def endpoint(name):
    value = getattr(s, name, None) or ''
    host = urlsplit(value).hostname or ''
    placeholder = bool(value and (
        host.endswith(('.invalid', '.example', '.test')) or
        host in ('localhost', '127.0.0.1', 'jenkins', 'harbor') or
        any(part in value.lower() for part in ('placeholder', 'change-me', 'replace-me'))))
    return {'set': bool(value), 'placeholder_or_local': placeholder}
print(json.dumps({
    'client_id': s.worker_name, 'client_secret': s.worker_secret,
    'gitlab_configured': bool(s.gitlab_url and s.gitlab_token),
    'jenkins': endpoint('jenkins_url'), 'harbor': endpoint('harbor_url'),
    'harbor_credentials_set': bool(s.harbor_user and s.harbor_password),
    'storage_backend': s.storage_backend, 'task_dispatcher': s.task_dispatcher,
}))
'''


class Acceptance:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        self.results: list[dict] = []

    def call(self, method: str, path: str, **kwargs):
        return self.session.request(method, self.base_url + path, timeout=25, **kwargs)

    def check(self, name: str, valid: bool, *, appid: str | None = None) -> None:
        self.results.append({"name": name, "passed": bool(valid), **({"appid": appid} if appid else {})})
        if not valid:
            raise RuntimeError(f"Acceptance failed: {name}" + (f" ({appid})" if appid else ""))

    def expect(self, method: str, path: str, status: int, label: str, *, appid=None, **kwargs):
        response = self.call(method, path, **kwargs)
        self.check(label, response.status_code == status, appid=appid)
        return response.json() if response.content else None


def safe_payload(app: dict, label: str) -> dict:
    if app.get("environment_keys"):
        raise RuntimeError("Sample requires environment secrets; explicit migration is needed")
    parsed = urlsplit(app.get("repository_url") or "")
    if parsed.password or parsed.username or parsed.query or parsed.fragment:
        raise RuntimeError("Sample repository contains non-public credentials or URL parameters")
    compose = deepcopy(app.get("compose"))
    if not isinstance(compose, dict) or not compose.get("services"):
        raise RuntimeError("Recent sample has no explicit Compose document")
    for service in compose["services"].values():
        if any(service.get(key) for key in ("environment", "env_file", "secrets", "configs")):
            raise RuntimeError("Sample Compose references sensitive configuration; not copied")
    if app.get("require_route"):
        raise RuntimeError("This legacy-compatibility test only imports apps without automatic route tasks")
    suffix = app["appid"].rsplit("-", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9]{4,20}", suffix):
        raise RuntimeError("Sample App ID has no safe, unique suffix")
    return {
        "appid": f"TEST110-{label}-{suffix}",
        "name": ("[110测试] " + app["name"])[:128],
        "repository_url": app.get("repository_url") or "",
        "git_ref": app.get("git_ref") or "main",
        "compose": compose, "environment": {},
        "components": app.get("components") or [], "require_route": None,
    }


def exercise_app(api: Acceptance, payload: dict, source: dict, baseline: dict | None,
                 config: dict) -> dict:
    appid = payload["appid"]
    encoded = quote(appid, safe="")
    info_path = f"/api/v1/jenkins/app_info/{encoded}"
    if baseline:
        same = all(baseline.get(key) == value for key, value in payload.items() if key != "environment")
        api.check("reuse-only-matching-isolated-app", same, appid=appid)
    else:
        api.expect("POST", "/api/v1/jenkins/app_create", 201, "import-recent-app", appid=appid, json=payload)
    actual = api.expect("GET", info_path, 200, "read-imported-app", appid=appid)
    api.check("app-config-roundtrip", all(actual.get(key) == value for key, value in payload.items() if key != "environment"), appid=appid)
    api.check("environment-secrets-not-exported", "environment_ciphertext" not in actual and actual["environment_keys"] == [], appid=appid)
    api.expect("POST", "/api/v1/jenkins/app_create", 409, "duplicate-app-rejected", appid=appid, json=payload)

    history_path = "/api/v1/jenkins/build_list"
    history = api.expect("GET", history_path, 200, "scoped-history-readable", appid=appid, params={"app_id": appid})
    api.check("no-source-build-success-migrated", history["total"] == 0 and not history["items"], appid=appid)
    api.expect("GET", history_path, 200, "status-and-pagination-filter", appid=appid,
               params={"app_id": appid, "status": "failed", "page": 1, "page_size": 10})

    patched = api.expect("PATCH", info_path, 200, "edit-isolated-app-name", appid=appid, json={"name": payload["name"] + " / 验收"})
    api.check("edited-name-visible", patched["name"].endswith(" / 验收"), appid=appid)
    api.expect("PATCH", info_path, 200, "restore-isolated-app-name", appid=appid, json={"name": payload["name"]})
    api.expect("PATCH", info_path, 422, "invalid-route-rejected", appid=appid, json={"require_route": "//unsafe"})

    rejected = api.expect("POST", f"/api/v1/jenkins/build_create/{encoded}", 422,
                          "legacy-app-build-fails-closed", appid=appid, json={})
    api.check("build-gate-explains-static-prerequisite", rejected.get("detail") == BUILD_GATE_ERROR, appid=appid)
    rejected = api.expect("POST", f"/api/v1/apps/{encoded}/static-checks", 422,
                          "static-submit-without-route-rejected", appid=appid, json={})
    api.check("static-prerequisite-explained", rejected.get("detail") == "App require_route is not configured", appid=appid)
    if not config["gitlab_configured"]:
        rejected = api.expect("PATCH", info_path, 422, "route-edit-without-gitlab-rejected", appid=appid,
                              json={"require_route": "/"})
        api.check("missing-gitlab-explained", rejected.get("detail") == GITLAB_ERROR, appid=appid)
        unchanged = api.expect("GET", info_path, 200, "read-after-rejected-edit", appid=appid)
        api.check("rejected-route-edit-not-saved", unchanged["require_route"] is None, appid=appid)

    sha = source.get("compose_source_commit")
    if sha:
        checks = api.expect("GET", f"/api/v1/apps/{encoded}/commits/{sha}/check-results", 200,
                            "source-commit-check-results-readable", appid=appid)
        api.check("checks-not-fabricated", checks == [], appid=appid)
    api.expect("GET", f"/api/v1/apps/{encoded}/commits/not-a-sha/check-results", 422,
               "invalid-commit-rejected", appid=appid)
    api.expect("POST", f"/api/v1/apps/{encoded}/dynamic-checks", 404,
               "missing-build-dynamic-check-rejected", appid=appid, json={"build_id": "acceptance-missing-build"})

    workspace_path = f"/api/v1/workspace/{encoded}"
    workspace = api.expect("GET", workspace_path, 200, "workspace-list-readable", appid=appid)
    api.check("fresh-sample-workspace-empty", workspace["appid"] == appid and not workspace["files"], appid=appid)
    marker_name = "acceptance-provenance.txt"
    marker = (json.dumps({"source_appid": source["appid"], "target_appid": appid}) + "\n").encode()
    try:
        api.expect("POST", workspace_path, 201, "workspace-upload", appid=appid,
                   files={"files": (marker_name, marker, "text/plain")})
        listing = api.expect("GET", workspace_path, 200, "workspace-read-after-upload", appid=appid)
        files = listing["files"]
        api.check("workspace-bytes-and-sha256", len(files) == 1 and files[0]["path"] == marker_name
                  and files[0]["size"] == len(marker) and files[0]["sha256"] == hashlib.sha256(marker).hexdigest(), appid=appid)
    finally:
        api.expect("DELETE", workspace_path, 204, "remove-only-own-workspace-marker", appid=appid,
                   params={"path": marker_name})
    listing = api.expect("GET", workspace_path, 200, "workspace-clean-after-test", appid=appid)
    api.check("no-test-upload-left-behind", not listing["files"] and listing["total_bytes"] == 0, appid=appid)

    for resource in ("events", "images", "services", "alerts"):
        value = api.expect("GET", f"/api/v1/jenkins/app_{resource}/{encoded}", 200,
                           f"runtime-{resource}-isolated", appid=appid)
        api.check(f"no-source-{resource}-migrated", value == [], appid=appid)
    access = api.expect("GET", f"/api/v1/jenkins/app_access/{encoded}", 200, "runtime-access-readable", appid=appid)
    api.check("no-false-deployment-access", access["appid"] == appid and not access["access_available"] and not access["services"], appid=appid)
    final_history = api.expect("GET", history_path, 200, "history-after-gate-tests", appid=appid, params={"app_id": appid})
    api.check("negative-tests-created-no-builds", final_history["total"] == 0, appid=appid)
    return {"appid": appid, "name": payload["name"], "git_ref": payload["git_ref"],
            "require_route": None, "source_appid": source["appid"],
            "source_updated_at": source["updated_at"], "source_commit": sha,
            "service_names": list(payload["compose"]["services"]),
            "source_metadata_not_imported": ["created_at", "updated_at", "compose_source_file", "compose_source_commit"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-host", default="10.32.12.110")
    parser.add_argument("--source-container", default="orchestrator-test-api-1")
    parser.add_argument("--target-host", default="10.32.12.111")
    parser.add_argument("--target-container", default="orchestrator-route-checks-api-1")
    parser.add_argument("--target-url", default="http://10.32.12.111:8088")
    parser.add_argument("--count", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--run-label", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--with-browser", action="store_true")
    args = parser.parse_args()
    if args.source_host == args.target_host or not re.fullmatch(r"[0-9]{8}", args.run_label):
        raise RuntimeError("Use distinct hosts and an eight-digit run label")
    target_origin = urlsplit(args.target_url)
    if (target_origin.hostname != args.target_host or target_origin.username or target_origin.password
            or target_origin.query or target_origin.fragment or target_origin.path not in ("", "/")):
        raise RuntimeError("Target URL must identify the target SSH host without credentials or extra URL components")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {"source_host": args.source_host, "target_url": args.target_url,
              "started_at": datetime.now().astimezone().isoformat(), "passed": False,
              "limits": ["No integration credentials or workspace contents migrated", "No source mutations",
                         "No imported source build/service/success records", "No build/model/deployment execution",
                         "Legacy route prerequisite and missing-GitLab rejection tested"]}
    api = Acceptance(args.target_url)
    try:
        source_code = SOURCE_EXPORT.replace("SAMPLE_COUNT", str(args.count))
        source = remote_json(args.source_host, args.source_container, source_code)
        report["source_total_apps"] = source["total_apps"]
        report["source_recent_builds"] = source["metadata"]
        payloads = [safe_payload(app, args.run_label) for app in source["apps"]]
        api.check("selected-apps-have-distinct-isolated-ids", len({p["appid"] for p in payloads}) == len(payloads))
        config = remote_json(args.target_host, args.target_container, TARGET_CONFIG)
        report["target_dependencies"] = {key: value for key, value in config.items() if key not in ("client_id", "client_secret")}
        api.expect("GET", "/healthz", 200, "target-health")
        auth = api.expect("POST", "/oauth2/token", 200, "real-client-login", json={"client_id": config["client_id"], "client_secret": config["client_secret"]})
        api.session.headers["Authorization"] = "Bearer " + auth["access_token"]
        before = api.expect("GET", "/api/v1/jenkins/app_list", 200, "target-app-list-before")
        existing = {app["appid"]: app for app in before}
        report["target_apps_before"] = len(before)
        imported = []
        report["samples"] = imported
        for payload, original in zip(payloads, source["apps"]):
            imported.append(exercise_app(api, payload, original, existing.get(payload["appid"]), config))
        after = api.expect("GET", "/api/v1/jenkins/app_list", 200, "target-app-list-after")
        after_by_id = {app["appid"]: app for app in after}
        api.check("all-test-apps-are-listed", all(payload["appid"] in after_by_id for payload in payloads))
        api.check("preexisting-unrelated-apps-unchanged", all(after_by_id.get(appid) == app for appid, app in existing.items() if appid not in {p["appid"] for p in payloads}))
        report["target_apps_after"] = len(after)
        roles = api.expect("GET", "/api/v1/agent-roles", 200, "route-agent-roles-readable")
        api.check("both-route-roles-present", {r["role_key"] for r in roles} == {"static_build_agent", "dynamic_debug_agent"})
        source_after = remote_json(args.source_host, args.source_container, source_code)
        api.check("source-apps-unchanged", source_after == source)
        if args.with_browser:
            browser_input = {"base_url": args.target_url, "client_id": config["client_id"],
                             "client_secret": config["client_secret"], "apps": imported,
                             "output_dir": str(args.output_dir / "browser")}
            browser = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_imported_apps_webui.py")],
                                     input=json.dumps(browser_input), text=True, capture_output=True,
                                     timeout=240, check=False)
            try:
                report["browser"] = json.loads(browser.stdout)
            except json.JSONDecodeError:
                raise RuntimeError("Browser check failed to return a safe JSON report; output withheld") from None
            api.check("real-browser-acceptance", browser.returncode in (0, 2)
                      and report["browser"].get("status") in ("passed", "passed_with_warnings"))
            report["status"] = report["browser"]["status"]
        report["passed"] = True
        report.setdefault("status", "passed")
    except Exception as error:
        # Never print transport errors, response bodies, or subprocess stderr:
        # those may include credentials, tokens, or sensitive app configuration.
        report["status"] = "failed"
        report["error"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
    finally:
        report["checks"] = api.results
        report["passed_checks"] = sum(item["passed"] for item in api.results)
        report["total_checks"] = len(api.results)
        report["finished_at"] = datetime.now().astimezone().isoformat()
        (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
