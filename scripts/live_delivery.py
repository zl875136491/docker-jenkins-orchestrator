"""Run an isolated live delivery audit against Jenkins, Harbor and Swarm.

The script is intentionally opt-in and is not part of the normal test suite.
Credentials are read from the operator-provided auth file and never included
in the result document. Every app, Mongo record, Redis queue, Harbor
repository, Jenkins build and Swarm object is namespaced by the generated run
identifier. Cleanup is the default; ``--retain-evidence`` keeps the Jenkins
job/build/artifact and Harbor repository/tag for operator inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import docker
import requests
import urllib3
import yaml
from cryptography.fernet import Fernet
from pymongo import MongoClient
from redis import Redis


ROOT = Path(__file__).resolve().parents[1]
AUTH_FILE = Path(os.environ.get("LIVE_E2E_AUTH_FILE", "/worker_space/orchestrator/auth.txt"))
PROJECTS = {
    "umami": ("Umami live delivery", "https://github.com/umami-software/umami.git", "master"),
    "planka": ("Planka live delivery", "https://github.com/plankanban/planka.git", "master"),
    "linkwarden": ("Linkwarden live delivery", "https://github.com/linkwarden/linkwarden.git", "main"),
}


def args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", default=os.environ.get("LIVE_E2E_JENKINS_JOB"))
    parser.add_argument("--run", default=os.environ.get("LIVE_E2E_RUN", uuid.uuid4().hex[:12]))
    parser.add_argument("--api-port", type=int, default=int(os.environ.get("LIVE_E2E_API_PORT", "18080")))
    parser.add_argument(
        "--mongo-url",
        default=os.environ.get("LIVE_E2E_MONGODB_URL") or os.environ.get("ORCHESTRATOR_MONGODB_URL"),
        help="MongoDB URL supplied through the operator environment",
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("LIVE_E2E_REDIS_URL") or os.environ.get("ORCHESTRATOR_CELERY_BROKER_URL"),
        help="Redis broker URL supplied through the operator environment",
    )
    parser.add_argument(
        "--projects",
        default=os.environ.get("LIVE_E2E_PROJECTS", ",".join(PROJECTS)),
        help="Comma-separated project fixture names to exercise (default: all fixtures)",
    )
    parser.add_argument(
        "--retain-evidence",
        action="store_true",
        help="Keep Jenkins job/build/artifact and Harbor repository/tag after a successful audit",
    )
    parser.add_argument("--result", type=Path, default=None)
    parsed = parser.parse_args()
    if not parsed.job:
        raise SystemExit("--job or LIVE_E2E_JENKINS_JOB is required")
    parsed.projects = tuple(item.strip() for item in parsed.projects.split(",") if item.strip())
    unknown = sorted(set(parsed.projects) - set(PROJECTS))
    if unknown:
        raise SystemExit(f"Unknown project fixture(s): {', '.join(unknown)}")
    if not parsed.projects:
        raise SystemExit("--projects must contain at least one project")
    if not parsed.mongo_url:
        raise SystemExit("--mongo-url or LIVE_E2E_MONGODB_URL is required")
    if not parsed.redis_url:
        raise SystemExit("--redis-url or LIVE_E2E_REDIS_URL is required")
    return parsed


def read_auth() -> dict:
    return yaml.safe_load(AUTH_FILE.read_text(encoding="utf-8"))


def main() -> int:
    options = args()
    auth = read_auth()
    jenkins = auth["jenkins"]
    harbor = auth["harbor"]
    jenkins_admin = auth["jenkins admin auth"]
    harbor_admin = auth["harbor admin auth"]
    run = options.run
    mongo_db = f"orchestrator_live_{run}"
    redis_db = 15
    queue_build = f"live-e2e-{run}.builds"
    queue_images = f"live-e2e-{run}.images"
    worker_name = f"live-e2e-{run}"
    worker_secret = uuid.uuid4().hex
    jwt_secret = uuid.uuid4().hex + uuid.uuid4().hex
    data_key = Fernet.generate_key().decode()
    mongo_url = options.mongo_url
    redis_url = options.redis_url
    api_url = f"http://127.0.0.1:{options.api_port}"
    env = os.environ.copy()
    env.update(
        {
            "ORCHESTRATOR_ENVIRONMENT": "production",
            "ORCHESTRATOR_WORKER_NAME": worker_name,
            "ORCHESTRATOR_WORKER_SECRET": worker_secret,
            "ORCHESTRATOR_JWT_SECRET": jwt_secret,
            "ORCHESTRATOR_STORAGE_BACKEND": "mongo",
            "ORCHESTRATOR_MONGODB_URL": mongo_url,
            "ORCHESTRATOR_MONGODB_DATABASE": mongo_db,
            "ORCHESTRATOR_DATA_ENCRYPTION_KEY": data_key,
            "ORCHESTRATOR_TASK_DISPATCHER": "celery",
            "ORCHESTRATOR_CELERY_BROKER_URL": redis_url,
            "ORCHESTRATOR_CELERY_BUILD_QUEUE": queue_build,
            "ORCHESTRATOR_CELERY_IMAGES_QUEUE": queue_images,
            "ORCHESTRATOR_CELERY_POLL_INTERVAL_SECONDS": "1",
            "ORCHESTRATOR_CELERY_MAX_POLL_ATTEMPTS": "120",
            "ORCHESTRATOR_CELERY_RECOVERY_INTERVAL_SECONDS": "3600",
            "ORCHESTRATOR_JENKINS_URL": "http://10.17.158.156",
            "ORCHESTRATOR_JENKINS_USER": jenkins["user"],
            "ORCHESTRATOR_JENKINS_PASSWORD": jenkins["passwd"],
            "ORCHESTRATOR_JENKINS_JOB_NAME": f"apps-orchestrator/{options.job}",
            "ORCHESTRATOR_HARBOR_URL": "https://10.17.158.118",
            "ORCHESTRATOR_HARBOR_USER": harbor["user"],
            "ORCHESTRATOR_HARBOR_PASSWORD": harbor["passwd"],
            "ORCHESTRATOR_HARBOR_PROJECT": "apps-orchestrator",
            "ORCHESTRATOR_DOCKER_BASE_URL": "unix:///var/run/docker.sock",
            "ORCHESTRATOR_DOCKER_SERVICES_NETWORK": "livee2e",
            "ORCHESTRATOR_EXTERNAL_REQUEST_TIMEOUT_SECONDS": "30",
            "PYTHONUNBUFFERED": "1",
        }
    )
    log_paths = {name: Path(f"/tmp/{run}-{name}.log") for name in ("api", "worker", "redis-monitor")}
    streams = {name: path.open("w", encoding="utf-8") for name, path in log_paths.items()}
    monitor = api = worker = None
    mongo = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
    redis = Redis.from_url(redis_url, decode_responses=True)
    docker_client = docker.from_env()
    jenkins_sessions = {admin: requests.Session() for admin in (False, True)}
    results: list[dict] = []
    # Register a round before its first API call.  A failed deployment can
    # otherwise leave an app, build, Harbor repository, or Swarm object behind
    # because the old code only retained fully successful rounds.
    rounds: list[dict[str, object]] = []

    def jreq(method: str, path: str, *, admin: bool = False, **kwargs):
        credentials = jenkins_admin if admin else jenkins
        url = path if path.startswith("http") else f"http://10.17.158.156{path}"
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("auth", (credentials["user"], credentials["passwd"]))
        return jenkins_sessions[admin].request(method, url, **kwargs)

    def hreq(method: str, path: str, *, admin: bool = False, **kwargs):
        url = path if path.startswith("http") else f"https://10.17.158.118{path}"
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("verify", False)
        credentials = harbor_admin if admin else harbor
        kwargs.setdefault("auth", (credentials["user"], credentials["passwd"]))
        return requests.request(method, url, **kwargs)

    def crumb() -> tuple[str, str]:
        response = jreq("GET", "/crumbIssuer/api/json", admin=True)
        response.raise_for_status()
        payload = response.json()
        return payload["crumbRequestField"], payload["crumb"]

    def delete_harbor(appid: str) -> bool:
        endpoint = f"/api/v2.0/projects/apps-orchestrator/repositories/{quote(appid, safe='')}"
        response = hreq("DELETE", endpoint, admin=True)
        if response.status_code not in (200, 202, 204, 404):
            raise RuntimeError(f"Harbor cleanup failed: HTTP {response.status_code}")
        for _ in range(20):
            if hreq("GET", endpoint, admin=True).status_code == 404:
                return True
            time.sleep(0.5)
        return False

    def delete_swarm(appid: str, refs: list[str]) -> list[str]:
        """Remove one app namespace and wait for Swarm allocator quiescence."""

        names: list[str] = []
        services_filter = {"label": f"io.docker-jenkins-orchestrator.appid={appid}"}
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                services = docker_client.services.list(filters=services_filter)
            except Exception:
                services = []
            for service in services:
                if service.name not in names:
                    names.append(service.name)
                try:
                    service.remove()
                except Exception:
                    pass
            if not services:
                break
            time.sleep(0.5)

        network_deadline = time.time() + 60
        while time.time() < network_deadline:
            try:
                networks = docker_client.networks.list(
                    filters={"label": f"io.docker-jenkins-orchestrator.appid={appid}"}
                )
            except Exception:
                networks = []
            if not networks:
                break
            for network in networks:
                try:
                    network.remove()
                except Exception:
                    # Docker may retain an attachment briefly after a service
                    # is removed. Retry until the allocator has released it.
                    pass
            time.sleep(0.5)
        for ref in refs:
            try:
                docker_client.images.remove(ref)
            except Exception:
                pass
        return names

    def wait_for_swarm_quiescence(appid: str, timeout: float = 60) -> bool:
        """Confirm no app services or attached temporary network remain."""

        services_filter = {"label": f"io.docker-jenkins-orchestrator.appid={appid}"}
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                services = docker_client.services.list(filters=services_filter)
            except Exception:
                services = []
            try:
                networks = docker_client.networks.list(
                    filters={"label": f"io.docker-jenkins-orchestrator.appid={appid}"}
                )
                containers = {
                    network.name: (network.attrs or {}).get("Containers") or {}
                    for network in networks
                }
            except Exception:
                networks = []
                containers = {}
            if not services and not networks and not containers:
                return True
            time.sleep(0.5)
        return False

    def delete_mongo(appid: str, build_id: str | None) -> dict[str, int]:
        database = mongo[mongo_db]
        deleted = {}
        for collection in ("user_apps", "build_jobs", "app_events", "user_images", "deployment_services", "alerts"):
            if collection == "user_apps" or build_id is None:
                query = {"appid": appid}
            else:
                query = {"$or": [{"appid": appid}, {"build_id": build_id}]}
            deleted[collection] = database[collection].delete_many(query).deleted_count
        return deleted

    def delete_build(number: int | None) -> bool:
        if number is None:
            return False
        field, value = crumb()
        response = jreq(
            "POST",
            f"/job/apps-orchestrator/job/{quote(options.job, safe='')}/{number}/doDelete",
            admin=True,
            headers={field: value},
            allow_redirects=False,
        )
        return response.status_code in (200, 201, 202, 204, 302, 303)

    def cleanup_round(round_context: dict[str, object]) -> None:
        """Best-effort cleanup for one round, including partially-created work."""

        appid = round_context["appid"]
        if not isinstance(appid, str):
            return
        build_id = round_context.get("build_id")
        build_id_value = build_id if isinstance(build_id, str) else None
        number = round_context.get("jenkins_build_number")
        number_value = number if isinstance(number, int) else None
        refs = round_context.get("refs")
        refs_value = refs if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs) else []
        try:
            round_context["swarm_services"] = delete_swarm(appid, refs_value)
            round_context["swarm_quiescent"] = wait_for_swarm_quiescence(appid)
        except Exception:
            round_context["swarm_quiescent"] = False
        if options.retain_evidence:
            round_context["harbor_retained"] = True
        else:
            try:
                round_context["harbor_deleted"] = delete_harbor(appid)
            except Exception:
                pass
        try:
            round_context["mongo_deleted"] = delete_mongo(appid, build_id_value)
        except Exception:
            pass
        if options.retain_evidence:
            round_context["jenkins_build_retained"] = number_value is not None
        else:
            try:
                round_context["jenkins_build_deleted"] = delete_build(number_value)
            except Exception:
                pass

    def capture_swarm_diagnostics(round_context: dict[str, object]) -> Path | None:
        """Write safe service/task state for a failed round without env values."""

        appid = round_context.get("appid")
        if not isinstance(appid, str):
            return None
        services = []
        try:
            candidates = docker_client.services.list(
                filters={"label": f"io.docker-jenkins-orchestrator.appid={appid}"}
            )
            for service in candidates:
                attrs = getattr(service, "attrs", {})
                spec = attrs.get("Spec", {}) if isinstance(attrs, dict) else {}
                task_template = spec.get("TaskTemplate", {}) if isinstance(spec, dict) else {}
                container = task_template.get("ContainerSpec", {}) if isinstance(task_template, dict) else {}
                services.append(
                    {
                        "name": getattr(service, "name", None),
                        "id": getattr(service, "id", None),
                        "image": container.get("Image") if isinstance(container, dict) else None,
                        "mode": spec.get("Mode") if isinstance(spec, dict) else None,
                        "endpoint_spec": spec.get("EndpointSpec") if isinstance(spec, dict) else None,
                        "labels": spec.get("Labels") if isinstance(spec, dict) else None,
                        "tasks": [
                            {
                                key: task.get(key)
                                for key in ("ID", "Name", "DesiredState", "Status")
                                if key in task
                            }
                            for task in docker_client.api.tasks(filters={"service": service.id})
                        ],
                    }
                )
        except Exception as exc:
            services.append({"diagnostic_error": type(exc).__name__})
        payload = {
            "run": run,
            "project": round_context.get("project"),
            "appid": appid,
            "build_id": round_context.get("build_id"),
            "jenkins_build_number": round_context.get("jenkins_build_number"),
            "services": services,
        }
        path = Path(f"/tmp/{run}-{round_context.get('project', 'round')}-swarm-diagnostics.json")
        try:
            path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
        except OSError:
            return None
        return path

    try:
        if redis.dbsize():
            raise RuntimeError(f"Redis database {redis_db} is not empty; refusing to risk shared data")
        monitor_env = env.copy()
        redis_password = urlsplit(redis_url).password
        if redis_password:
            monitor_env["REDISCLI_AUTH"] = unquote(redis_password)
        monitor = subprocess.Popen(
            ["redis-cli", "-n", str(redis_db), "MONITOR"],
            cwd=ROOT,
            env=monitor_env,
            stdout=streams["redis-monitor"],
            stderr=subprocess.STDOUT,
        )
        api = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(options.api_port)],
            cwd=ROOT,
            env=env,
            stdout=streams["api"],
            stderr=subprocess.STDOUT,
        )
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "orchestrator.celery_runtime:celery_app",
                "worker",
                "--loglevel=INFO",
                "--pool=solo",
                "--concurrency=1",
                "--hostname",
                f"{worker_name}@%h",
                "--queues",
                f"{queue_build},{queue_images}",
            ],
            cwd=ROOT,
            env=env,
            stdout=streams["worker"],
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                if requests.get(f"{api_url}/healthz", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("API did not become ready")
        deadline = time.time() + 45
        while time.time() < deadline:
            if worker.poll() is not None:
                raise RuntimeError("Celery worker exited before readiness")
            if " ready." in log_paths["worker"].read_text(errors="replace"):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Celery worker did not report readiness")

        connection = requests.post(
            f"{api_url}/api/connect", json={"worker_name": worker_name, "worker_secret": worker_secret}, timeout=10
        )
        connection.raise_for_status()
        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {connection.json()['access_token']}"})
        session.get(f"{api_url}/api/me", timeout=10).raise_for_status()

        for project in options.projects:
            name, repository_url, git_ref = PROJECTS[project]
            appid = f"{project}-{uuid.uuid4().hex}"
            round_context: dict[str, object] = {
                "project": project,
                "appid": appid,
                "build_id": None,
                "jenkins_build_number": None,
                "refs": [],
            }
            rounds.append(round_context)
            compose = yaml.safe_load(
                (ROOT / "tests" / "fixtures" / "complex_projects" / project / "user-compose.yml").read_text()
            )
            app_response = session.post(
                f"{api_url}/api/apps",
                json={
                    "appid": appid,
                    "name": name,
                    "repository_url": repository_url,
                    "git_ref": git_ref,
                    "environment": {"LIVE_E2E_PROJECT": project, "LIVE_E2E_RUN": run},
                    "compose": compose,
                    "components": [],
                },
                timeout=10,
            )
            app_response.raise_for_status()
            public_app = app_response.json()
            if "LIVE_E2E_RUN" in public_app.get("environment_keys", []) and "LIVE_E2E_PROJECT" in public_app.get(
                "environment_keys", []
            ):
                # Keys are intentionally exposed for UI rendering; values must
                # remain encrypted and absent from the public response.
                pass
            if run in json.dumps(public_app):
                raise RuntimeError("API leaked environment values")
            build = session.post(f"{api_url}/api/apps/{appid}/builds", json={}, timeout=10)
            build.raise_for_status()
            build_data = build.json()
            build_id = build_data["build_id"]
            round_context["build_id"] = build_id
            statuses = [build_data["status"]]
            started = time.time()
            while time.time() - started < 180:
                current = session.get(f"{api_url}/api/builds/{build_id}", timeout=10)
                current.raise_for_status()
                payload = current.json()
                if payload["status"] != statuses[-1]:
                    statuses.append(payload["status"])
                if payload["status"] in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(0.75)
            else:
                raise RuntimeError(f"{project} build timed out")
            if payload["status"] != "succeeded":
                raise RuntimeError(f"{project} build failed: {payload.get('error') or payload['status']}")

            events = session.get(f"{api_url}/api/apps/{appid}/events", timeout=10).json()
            images = session.get(f"{api_url}/api/apps/{appid}/images", timeout=10).json()
            services = session.get(f"{api_url}/api/apps/{appid}/services", timeout=10).json()
            alerts = session.get(f"{api_url}/api/apps/{appid}/alerts", timeout=10).json()
            expected = len(compose["services"])
            if len(images) != expected or len(services) != expected or alerts:
                raise RuntimeError(f"{project} repository counts are invalid")

            number = payload["jenkins_build_number"]
            round_context["jenkins_build_number"] = number
            jbuild = jreq(
                "GET",
                f"/job/apps-orchestrator/job/{quote(options.job, safe='')}/{number}/api/json?tree=building,result,duration",
            )
            jbuild.raise_for_status()
            artifact_response = jreq(
                "GET",
                f"/job/apps-orchestrator/job/{quote(options.job, safe='')}/{number}/artifact/orchestrator-result.json",
            )
            artifact_response.raise_for_status()
            artifact = artifact_response.json()
            if jbuild.json().get("building") or jbuild.json().get("result") != "SUCCESS":
                raise RuntimeError(f"{project} Jenkins build was not successful")
            if len(artifact.get("images", [])) != expected or set(artifact.get("compose", {}).get("services", {})) != set(
                compose["services"]
            ):
                raise RuntimeError(f"{project} artifact topology mismatch")

            repository = f"apps-orchestrator/{appid}"
            tags = hreq("GET", f"/v2/{repository}/tags/list")
            tags.raise_for_status()
            tags_list = tags.json().get("tags") or []
            refs = [item["reference"] for item in images]
            round_context["refs"] = refs
            digests = []
            for ref in refs:
                tag = ref.rsplit(":", 1)[-1]
                manifest = hreq(
                    "GET",
                    f"/v2/{repository}/manifests/{quote(tag, safe='')}",
                    headers={"Accept": "application/vnd.oci.image.manifest.v1+json"},
                )
                manifest.raise_for_status()
                digest = manifest.headers.get("Docker-Content-Digest")
                if not digest or not digest.startswith("sha256:"):
                    raise RuntimeError(f"{project} Harbor digest is missing")
                digests.append(digest)
            swarm_services = docker_client.services.list(
                filters={"label": f"io.docker-jenkins-orchestrator.appid={appid}"}
            )
            networks = docker_client.networks.list(
                filters={"label": f"io.docker-jenkins-orchestrator.appid={appid}"}
            )
            if len(swarm_services) != expected or len(networks) != 1:
                raise RuntimeError(f"{project} Swarm topology is invalid")
            network = networks[0]
            if network.attrs.get("Labels", {}).get("io.docker-jenkins-orchestrator.appid") != appid:
                raise RuntimeError(f"{project} Swarm network label is invalid")
            task_states = {}
            for service in swarm_services:
                for _ in range(30):
                    tasks = docker_client.api.tasks(filters={"service": service.id})
                    states = [task.get("Status", {}).get("State") for task in tasks]
                    if states and all(state == "running" for state in states):
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError(f"{project} Swarm task did not reach running")
                task_states[service.name] = states
            database = mongo[mongo_db]
            mongo_counts = {
                collection: database[collection].count_documents({"appid": appid})
                for collection in ("user_apps", "build_jobs", "app_events", "user_images", "deployment_services", "alerts")
            }
            if mongo_counts["user_apps"] != 1 or mongo_counts["build_jobs"] != 1 or mongo_counts["deployment_services"] != expected:
                raise RuntimeError(f"{project} Mongo persistence counts are invalid")

            cleanup_round(round_context)
            if round_context.get("swarm_quiescent") is not True:
                raise RuntimeError(f"{project} Swarm namespace did not become quiescent after cleanup")
            deleted_services = round_context.get("swarm_services", [])
            harbor_deleted = round_context.get("harbor_deleted", False)
            mongo_deleted = round_context.get("mongo_deleted", {})
            jenkins_deleted = round_context.get("jenkins_build_deleted", False)
            residual = [
                key
                for key in redis.scan_iter(match=f"live-e2e-{run}*")
            ] + [
                key
                for key in redis.scan_iter(match=f"_kombu.binding.live-e2e-{run}*")
            ]
            for key in residual:
                redis.delete(key)
            remaining_redis = [
                key for key in redis.scan_iter(match=f"live-e2e-{run}*")
            ] + [
                key for key in redis.scan_iter(match=f"_kombu.binding.live-e2e-{run}*")
            ]
            results.append(
                {
                    "project": project,
                    "appid": appid,
                    "build_id": build_id,
                    "jenkins_build_number": number,
                    "statuses": statuses,
                    "service_count": expected,
                    "event_count": len(events),
                    "mongo_counts_before_cleanup": mongo_counts,
                    "mongo_deleted": mongo_deleted,
                    "mongo_remaining_after_cleanup": {
                        collection: database[collection].count_documents({"appid": appid})
                        for collection in ("user_apps", "build_jobs", "app_events", "user_images", "deployment_services", "alerts")
                    },
                    "harbor_tags": sorted(tags_list),
                    "harbor_digests": digests,
                    "harbor_deleted": harbor_deleted,
                    "harbor_retained": bool(round_context.get("harbor_retained")),
                    "harbor_repository": repository,
                    "harbor_repository_api": (
                        f"https://10.17.158.118/api/v2.0/projects/apps-orchestrator/repositories/"
                        f"{quote(appid, safe='')}"
                    ),
                    "swarm_services": sorted(deleted_services) if isinstance(deleted_services, list) else [],
                    "swarm_task_states": task_states,
                    "redis_keys_deleted_during_cleanup": residual,
                    "redis_residual_keys_after_cleanup": remaining_redis,
                    "jenkins_build_deleted": jenkins_deleted,
                    "jenkins_build_retained": bool(round_context.get("jenkins_build_retained")),
                    "jenkins_build_url": (
                        f"http://10.17.158.156/job/apps-orchestrator/job/{quote(options.job, safe='')}/{number}/"
                    ),
                    "jenkins_artifact_url": (
                        f"http://10.17.158.156/job/apps-orchestrator/job/{quote(options.job, safe='')}/"
                        f"{number}/artifact/orchestrator-result.json"
                    ),
                }
            )
        streams["redis-monitor"].flush()
        monitor_lines = log_paths["redis-monitor"].read_text(errors="replace").splitlines()
        relevant = [line for line in monitor_lines if queue_build in line or queue_images in line]
        worker_text = log_paths["worker"].read_text(errors="replace")
        summary = {
            "run": run,
            "jenkins_job": options.job,
            "jenkins_job_url": f"http://10.17.158.156/job/apps-orchestrator/job/{quote(options.job, safe='')}/",
            "external_evidence_retained": options.retain_evidence,
            "api": api_url,
            "mongo_database": mongo_db,
            "redis_database": redis_db,
            "queues": [queue_build, queue_images],
            "redis_activity": {
                "monitor_lines_for_unique_queues": len(relevant),
                "enqueue_commands": sum("LPUSH" in line or "LPUSHX" in line for line in relevant),
                "consume_commands": sum("BRPOP" in line or "LPOP" in line for line in relevant),
            },
            "worker_activity": {
                "start_tasks_received": worker_text.count("orchestrator.pipeline.start_build"),
                "poll_tasks_received": worker_text.count("orchestrator.pipeline.poll_build"),
                "deploy_success_events": worker_text.count("build.deployed"),
            },
            "rounds": results,
        }
        result_path = options.result or Path(f"/tmp/{run}-results.json")
        result_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception:
        for round_context in rounds:
            if round_context.get("build_id") is not None or round_context.get("appid") is not None:
                diagnostic = capture_swarm_diagnostics(round_context)
                if diagnostic is not None:
                    print(f"Swarm diagnostics: {diagnostic}", file=sys.stderr)
        raise
    finally:
        for round_context in rounds:
            cleanup_round(round_context)
        if not options.retain_evidence:
            try:
                field, value = crumb()
                jreq(
                    "POST",
                    f"/job/apps-orchestrator/job/{quote(options.job, safe='')}/doDelete",
                    admin=True,
                    headers={field: value},
                )
            except Exception:
                pass
        try:
            mongo.drop_database(mongo_db)
        except Exception:
            pass
        try:
            for key in list(redis.scan_iter(match=f"live-e2e-{run}*")) + list(
                redis.scan_iter(match=f"_kombu.binding.live-e2e-{run}*")
            ):
                redis.delete(key)
        except Exception:
            pass
        for process in (worker, api, monitor):
            if process is not None and process.poll() is None:
                process.terminate()
        for process in (worker, api, monitor):
            if process is not None:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
        for stream in streams.values():
            stream.close()
        docker_client.close()
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
