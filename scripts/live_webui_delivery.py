"""Run real project deliveries through the test control console UI.

This script deliberately drives the browser rather than calling the control
API directly. It is an opt-in integration helper; credentials are read from
the generated test-environment dotenv file and never written to the result.
Independent Jenkins, Harbor, Swarm, Mongo, and HTTP checks should be made
after the UI run using the recorded app IDs and build IDs.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "complex_projects"
DEFAULT_API = "http://10.32.12.110:18080"
PROJECTS: dict[str, dict[str, Any]] = {
    "linkwarden": {
        "name": "Linkwarden live delivery",
        "repository_url": "https://github.com/linkwarden/linkwarden.git",
        "git_ref": "main",
        "port": 18084,
        "path": "/",
    },
    "open-webui": {
        "name": "Open WebUI live delivery",
        "repository_url": "https://github.com/open-webui/open-webui.git",
        "git_ref": "main",
        "port": 18085,
        "path": "/",
    },
    "planka": {
        "name": "Planka live delivery",
        "repository_url": "https://github.com/plankanban/planka.git",
        "git_ref": "master",
        "port": 18086,
        "path": "/",
    },
    "searxng": {
        "name": "SearXNG live delivery",
        "repository_url": "https://github.com/searxng/searxng.git",
        "git_ref": "master",
        "port": 18087,
        "path": "/",
    },
    "umami": {
        "name": "Umami live delivery",
        "repository_url": "https://github.com/umami-software/umami.git",
        "git_ref": "master",
        "port": 18088,
        "path": "/",
    },
}


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        values[key] = value
    return values


def replace_port(raw: Any, published: int) -> Any:
    if isinstance(raw, str):
        parts = raw.split(":")
        if len(parts) == 2:
            return f"{published}:{parts[1]}"
        if len(parts) == 3:
            return f"{published}:{parts[2]}"
        return raw
    if isinstance(raw, dict):
        value = dict(raw)
        value["published"] = published
        return value
    return raw


def delivery_compose(project: str, api_host: str) -> dict[str, Any]:
    document = yaml.safe_load((FIXTURES / project / "user-compose.yml").read_text(encoding="utf-8"))
    document = copy.deepcopy(document)
    config = PROJECTS[project]
    public_url = f"http://{api_host}:{config['port']}"
    for service in document["services"].values():
        if "ports" in service:
            service["ports"] = [replace_port(item, config["port"]) for item in service["ports"]]
    if project == "searxng":
        core = document["services"].get("searxng-core", {})
        core["volumes"] = [
            item
            for item in core.get("volumes", [])
            if not (isinstance(item, str) and item.startswith("/srv/searxng/core-config:"))
        ]
    if project == "linkwarden":
        document["services"]["linkwarden"]["environment"]["NEXTAUTH_URL"] = public_url
    elif project == "planka":
        document["services"]["planka"]["environment"]["BASE_URL"] = public_url
    elif project == "searxng":
        document["services"]["searxng-core"]["environment"]["SEARXNG_BASE_URL"] = f"{public_url}/"
    return document


def json_from_pre(page: Page, selector: str) -> Any:
    text = page.locator(selector).inner_text().strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def click_and_wait(page: Page, selector: str, *, timeout: float = 30_000) -> None:
    page.locator(selector).click()
    page.wait_for_timeout(250)


def navigate_section(page: Page, section: str) -> None:
    page.locator(f".side-nav a[href='#{section}']").click()
    page.wait_for_function(
        "document.querySelector(%s)?.classList.contains('active')" % json.dumps(f"#{section}"),
        timeout=30_000,
    )


def connect(page: Page, api_url: str, settings: dict[str, str]) -> None:
    page.goto(f"{api_url}/ui/#appSection", wait_until="networkidle", timeout=60_000)
    page.locator("#openConnectionModal").click()
    page.locator("#workerName").fill(settings["ORCHESTRATOR_WORKER_NAME"])
    page.locator("#workerSecret").fill(settings["ORCHESTRATOR_WORKER_SECRET"])
    page.locator("#connectForm button[type='submit']").click()
    page.wait_for_function(
        "document.querySelector('#sessionStatus') && document.querySelector('#sessionStatus').textContent.includes('已连接')",
        timeout=60_000,
    )


def create_app(page: Page, project: str, appid: str, compose: dict[str, Any], config: dict[str, Any], run: str) -> dict[str, Any]:
    navigate_section(page, "appSection")
    page.locator("#appId").fill(appid)
    page.locator("#appName").fill(config["name"])
    page.locator("#repositoryUrl").fill(config["repository_url"])
    page.locator("#gitRef").fill(config["git_ref"])
    page.locator("#components").fill("")
    page.locator("#environment").fill(json.dumps({"WEBUI_E2E_PROJECT": project, "WEBUI_E2E_RUN": run}))
    page.locator("#compose").fill(json.dumps(compose, ensure_ascii=True, indent=2))
    page.locator("#appForm button[type='submit']").click()
    page.wait_for_function(
        "document.querySelector('#appOutput').textContent.includes(%s)" % json.dumps(appid),
        timeout=60_000,
    )
    payload = json_from_pre(page, "#appOutput")
    if not isinstance(payload, dict) or payload.get("appid") != appid:
        raise RuntimeError(f"{project}: WebUI app creation response was invalid")
    return payload


def submit_build(page: Page, appid: str, git_ref: str) -> dict[str, Any]:
    navigate_section(page, "buildSection")
    page.locator("#buildGitRef").fill(git_ref)
    page.locator("#buildForm button[type='submit']").click()
    page.wait_for_function(
        "document.querySelector('#buildOutput').textContent.includes('build_id')",
        timeout=60_000,
    )
    payload = json_from_pre(page, "#buildOutput")
    if not isinstance(payload, dict) or payload.get("appid") != appid or not payload.get("build_id"):
        raise RuntimeError(f"{appid}: WebUI build response was invalid")
    return payload


def poll_build(page: Page, build_id: str, timeout_seconds: int) -> tuple[dict[str, Any], list[str]]:
    statuses: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        page.locator("#buildId").fill(build_id)
        page.locator("#pollBuildButton").click()
        try:
            page.wait_for_function(
                "document.querySelector('#buildOutput').textContent.includes('build_id')",
                timeout=15_000,
            )
        except PlaywrightTimeoutError:
            continue
        value = json_from_pre(page, "#buildOutput")
        if not isinstance(value, dict):
            continue
        payload = value
        status = str(value.get("status", ""))
        if status and (not statuses or statuses[-1] != status):
            statuses.append(status)
        if status in {"succeeded", "failed", "cancelled"}:
            return payload, statuses
        page.wait_for_timeout(5_000)
    raise RuntimeError(f"{build_id}: WebUI polling timed out after {timeout_seconds}s")


def inspect_history(page: Page, appid: str, build_id: str) -> dict[str, Any]:
    navigate_section(page, "historySection")
    page.locator("#historyAppId").fill(appid)
    page.locator("#historyFilterForm button[type='submit']").click()
    page.wait_for_function(
        "document.querySelectorAll('#historyTableBody tr').length > 0",
        timeout=60_000,
    )
    rows = page.locator("#historyTableBody tr")
    selected = None
    for index in range(rows.count()):
        if build_id in rows.nth(index).inner_text():
            selected = rows.nth(index)
            break
    if selected is None:
        raise RuntimeError(f"{appid}: build {build_id} did not appear in WebUI history")
    selected.locator(".history-detail-button").click()
    page.wait_for_function(
        "!document.querySelector('#historyDetailPanel').hidden && document.querySelector('#historyDetailBuild').textContent.includes('build_id')",
        timeout=60_000,
    )
    return {
        "build": json_from_pre(page, "#historyDetailBuild"),
        "events": json_from_pre(page, "#historyDetailEvents"),
        "images": json_from_pre(page, "#historyDetailImages"),
        "services": json_from_pre(page, "#historyDetailServices"),
        "alerts": json_from_pre(page, "#historyDetailAlerts"),
    }


def inspect_resources(page: Page, appid: str) -> dict[str, Any]:
    navigate_section(page, "resourcesSection")
    result: dict[str, Any] = {}
    for resource in ("events", "images", "services", "access", "alerts"):
        page.locator(f"button.resource-button[data-resource='{resource}']").click()
        page.wait_for_function(
            "document.querySelector('#resourceOutput').textContent.trim() && document.querySelector('#resourceTitle').textContent",
            timeout=60_000,
        )
        result[resource] = json_from_pre(page, "#resourceOutput")
    page.locator("#refreshAccessButton").click()
    page.wait_for_function(
        "document.querySelector('#resourceOutput').textContent.includes('access_urls') || document.querySelector('#resourceOutput').textContent.includes('access_reason')",
        timeout=60_000,
    )
    result["access_table_text"] = page.locator("#serviceAccessTableBody").inner_text()
    result["access_summary"] = page.locator("#serviceAccessSummary").inner_text()
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default=os.environ.get("LIVE_WEBUI_API_URL", DEFAULT_API))
    parser.add_argument("--env-file", type=Path, default=Path("/opt/orchestrator-test/.env"))
    parser.add_argument("--run", default=os.environ.get("LIVE_WEBUI_RUN", time.strftime("%Y%m%d%H%M%S")))
    parser.add_argument("--projects", default=",".join(PROJECTS))
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--result", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    options = parse_args()
    projects = tuple(item.strip() for item in options.projects.split(",") if item.strip())
    unknown = sorted(set(projects) - set(PROJECTS))
    if unknown:
        raise SystemExit(f"Unknown project(s): {', '.join(unknown)}")
    settings = read_dotenv(options.env_file)
    api_host = options.api_url.split("//", 1)[-1].split(":", 1)[0]
    results: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        executable = "/snap/bin/chromium" if Path("/snap/bin/chromium").exists() else playwright.chromium.executable_path
        browser = playwright.chromium.launch(headless=True, executable_path=executable, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        connect(page, options.api_url, settings)
        for project in projects:
            config = PROJECTS[project]
            appid = f"{project}-{uuid.uuid4().hex}"
            compose = delivery_compose(project, api_host)
            item: dict[str, Any] = {
                "project": project,
                "appid": appid,
                "public_port": config["port"],
                "compose": compose,
            }
            try:
                item["app"] = create_app(page, project, appid, compose, config, options.run)
                build = submit_build(page, appid, config["git_ref"])
                item["build_submission"] = build
                final, statuses = poll_build(page, build["build_id"], options.timeout_seconds)
                item["statuses"] = statuses
                item["build"] = final
                item["history_detail"] = inspect_history(page, appid, build["build_id"])
                if final.get("status") == "succeeded":
                    item["resources"] = inspect_resources(page, appid)
                item["status"] = final.get("status")
            except Exception as exc:  # retain evidence for other projects
                item["status"] = "automation_error"
                item["error"] = f"{type(exc).__name__}: {exc}"
            results.append(item)
        browser.close()
    output = {
        "run": options.run,
        "api_url": options.api_url,
        "projects": list(projects),
        "results": results,
    }
    result_path = options.result or Path(f"/tmp/{options.run}-webui-results.json")
    result_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all(item.get("status") == "succeeded" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
