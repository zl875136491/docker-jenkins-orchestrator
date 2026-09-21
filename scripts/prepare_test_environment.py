"""Prepare a credential-free-in-git external integration test environment.

The operator supplies ``auth.txt`` outside the repository. This script reads
the required credentials, generates ephemeral runtime secrets, and writes a
mode-0600 dotenv file under ``/opt/orchestrator-test`` by default. It never
prints credentials and never writes them into the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import requests
from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUTH_FILE = ROOT.parent / "auth.txt"
DEFAULT_RUNTIME_DIR = Path("/opt/orchestrator-test")


def parse_auth_file(path: Path) -> dict[str, dict[str, str]]:
    """Parse the small operator auth file, including its legacy Mongo syntax."""

    sections: dict[str, dict[str, str]] = {}
    section: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(":") and ":" not in line[:-1]:
            section = line[:-1]
            sections[section] = {}
            continue
        if section is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        sections[section][key.strip()] = value.strip().strip('"').strip("'")
    return sections


def required(auth: dict[str, dict[str, str]], section: str, key: str) -> str:
    value = auth.get(section, {}).get(key, "").strip()
    if not value:
        raise SystemExit(f"Missing {section}.{key} in auth file")
    return value


def dotenv_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def can_resolve(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None)
    except OSError:
        return False
    return True


def detect_advertise_host() -> str:
    """Return the host address peers can use to reach the test service.

    The UDP connect only asks the kernel which interface would route traffic;
    it does not send a packet. Fall back to loopback when no route is present.
    """

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("1.1.1.1", 80))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def choose_jenkins_job(base_url: str, user: str, password: str, requested: str | None) -> str:
    if requested:
        return requested.removeprefix("/job/")
    candidates = ("orchestrator-real-delivery-20260920",)
    try:
        response = requests.get(
            f"{base_url.rstrip('/')}/job/apps-orchestrator/api/json",
            params={"tree": "jobs[name]"},
            auth=(user, password),
            timeout=10,
        )
        response.raise_for_status()
        names = {item.get("name") for item in response.json().get("jobs", [])}
    except requests.RequestException as exc:
        raise SystemExit(f"Unable to discover Jenkins jobs: {exc.__class__.__name__}") from exc
    for candidate in candidates:
        if candidate in names:
            return f"apps-orchestrator/{candidate}"
    raise SystemExit(
        "The real orchestrator delivery Jenkins job is not available under apps-orchestrator; "
        "provision jenkins/orchestrator-build.groovy and pass --jenkins-job explicitly"
    )


def build_environment(auth: dict[str, dict[str, str]], args: argparse.Namespace) -> tuple[dict[str, str], str, bool]:
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    jenkins_host = args.jenkins_host
    harbor_host = args.harbor_host
    jenkins_url = f"http://{jenkins_host}"
    harbor_url = f"https://{harbor_host}"
    jenkins_user = required(auth, "jenkins", "user")
    jenkins_password = required(auth, "jenkins", "passwd")
    harbor_user = required(auth, "harbor", "user")
    harbor_password = required(auth, "harbor", "passwd")
    gitlab_url = auth.get("gitlab", {}).get("url", "").rstrip("/")
    gitlab_enabled = bool(gitlab_url and can_resolve(gitlab_url.split("//", 1)[-1].split("/", 1)[0]))
    job_name = choose_jenkins_job(jenkins_url, jenkins_user, jenkins_password, args.jenkins_job)
    redis_password = secrets.token_urlsafe(32)
    worker_name = f"orchestrator-test-{run_id}"
    database = f"orchestrator_test_{run_id}"
    public_host = args.api_advertise_host or (
        args.api_bind_address if args.api_bind_address not in {"", "0.0.0.0"} else detect_advertise_host()
    )
    env = {
        "ORCHESTRATOR_ENVIRONMENT": "production",
        "ORCHESTRATOR_WORKER_NAME": worker_name,
        "ORCHESTRATOR_WORKER_SECRET": secrets.token_urlsafe(32),
        "ORCHESTRATOR_JWT_SECRET": secrets.token_urlsafe(48),
        "ORCHESTRATOR_STORAGE_BACKEND": "mongo",
        "ORCHESTRATOR_MONGODB_URL": (
            f"mongodb://{quote_plus(required(auth, 'mongodb', 'user'))}:"
            f"{quote_plus(required(auth, 'mongodb', 'passwd'))}@"
            f"{required(auth, 'mongodb', 'host')}:{required(auth, 'mongodb', 'port')}/?authSource="
            f"{quote_plus(auth.get('mongodb', {}).get('authsource', 'admin'))}"
        ),
        "ORCHESTRATOR_MONGODB_DATABASE": database,
        "ORCHESTRATOR_DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ORCHESTRATOR_TASK_DISPATCHER": "celery",
        "ORCHESTRATOR_REDIS_PASSWORD": redis_password,
        "ORCHESTRATOR_CELERY_BUILD_QUEUE": f"{worker_name}.builds",
        "ORCHESTRATOR_CELERY_IMAGES_QUEUE": f"{worker_name}.images",
        "ORCHESTRATOR_CELERY_POLL_INTERVAL_SECONDS": "5",
        "ORCHESTRATOR_CELERY_MAX_POLL_ATTEMPTS": "120",
        "ORCHESTRATOR_CELERY_RECOVERY_INTERVAL_SECONDS": "60",
        "ORCHESTRATOR_JENKINS_URL": jenkins_url,
        "ORCHESTRATOR_JENKINS_USER": jenkins_user,
        "ORCHESTRATOR_JENKINS_PASSWORD": jenkins_password,
        "ORCHESTRATOR_JENKINS_JOB_NAME": job_name,
        "ORCHESTRATOR_HARBOR_URL": harbor_url,
        "ORCHESTRATOR_HARBOR_USER": harbor_user,
        "ORCHESTRATOR_HARBOR_PASSWORD": harbor_password,
        "ORCHESTRATOR_HARBOR_PROJECT": "apps-orchestrator",
        "ORCHESTRATOR_HARBOR_BOOT_IMAGES_PROJECT": "boot-images",
        "ORCHESTRATOR_DOCKER_BASE_URL": "unix:///var/run/docker.sock",
        "ORCHESTRATOR_DOCKER_SOCKET_PATH": "/var/run/docker.sock",
        "ORCHESTRATOR_DOCKER_SERVICES_NETWORK": f"{worker_name}-network",
        "ORCHESTRATOR_DEPLOYMENT_READINESS_TIMEOUT_SECONDS": "900",
        "ORCHESTRATOR_DEPLOYMENT_READINESS_POLL_INTERVAL_SECONDS": "1",
        "ORCHESTRATOR_EXTERNAL_REQUEST_TIMEOUT_SECONDS": "30",
        "ORCHESTRATOR_PUBLIC_HOST": public_host,
        "ORCHESTRATOR_PUBLIC_SCHEME": "http",
        "ORCHESTRATOR_API_HOST_PORT": str(args.api_port),
        "ORCHESTRATOR_API_BIND_ADDRESS": args.api_bind_address,
        "ORCHESTRATOR_REDIS_HOST_PORT": str(args.redis_port),
        "ORCHESTRATOR_TEST_DATA_PATH": str(args.runtime_dir),
        "ORCHESTRATOR_GITLAB_URL": gitlab_url if gitlab_enabled else "",
        "ORCHESTRATOR_GITLAB_TOKEN": auth.get("gitlab", {}).get("pat", "") if gitlab_enabled else "",
    }
    return env, database, gitlab_enabled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-file", type=Path, default=Path(os.environ.get("ORCHESTRATOR_AUTH_FILE", DEFAULT_AUTH_FILE)))
    parser.add_argument("--runtime-dir", type=Path, default=Path(os.environ.get("ORCHESTRATOR_TEST_DATA_PATH", DEFAULT_RUNTIME_DIR)))
    parser.add_argument("--run-id", default=os.environ.get("ORCHESTRATOR_TEST_RUN_ID"))
    parser.add_argument("--jenkins-host", default="10.17.158.156")
    parser.add_argument("--harbor-host", default="10.17.158.118")
    parser.add_argument("--jenkins-job", default=os.environ.get("ORCHESTRATOR_JENKINS_JOB_NAME"))
    parser.add_argument("--api-port", type=int, default=18080)
    parser.add_argument(
        "--api-bind-address",
        default=os.environ.get("ORCHESTRATOR_API_BIND_ADDRESS", detect_advertise_host()),
        help="host address for the test API/UI port mapping (default: detected host address)",
    )
    parser.add_argument(
        "--api-advertise-host",
        default=os.environ.get("ORCHESTRATOR_API_ADVERTISE_HOST"),
        help="host/IP to print in the access URL; defaults to the outbound interface",
    )
    parser.add_argument("--redis-port", type=int, default=16379)
    args = parser.parse_args()
    if not args.auth_file.is_file():
        raise SystemExit(f"Auth file not found: {args.auth_file}")
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.runtime_dir, 0o700)
    (args.runtime_dir / "redis").mkdir(exist_ok=True)
    (args.runtime_dir / "beat").mkdir(exist_ok=True)
    auth = parse_auth_file(args.auth_file)
    env, database, gitlab_enabled = build_environment(auth, args)
    env_path = args.runtime_dir / ".env"
    env_path.write_text("\n".join(f"{key}={dotenv_value(value)}" for key, value in env.items()) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    advertise_host = args.api_advertise_host
    if not advertise_host:
        advertise_host = (
            args.api_bind_address
            if args.api_bind_address not in {"", "0.0.0.0"}
            else detect_advertise_host()
        )
    metadata = {
        "database": database,
        "api_bind_address": args.api_bind_address,
        "api_url": f"http://{advertise_host}:{args.api_port}",
        "compose_file": str(ROOT / "docker-compose.test.yml"),
        "gitlab_validation_enabled": gitlab_enabled,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (args.runtime_dir / "environment.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Prepared test environment at {args.runtime_dir}")
    print(f"Mongo database: {database}")
    print(f"API URL: {metadata['api_url']}/ui/")
    print(f"GitLab validation: {'enabled' if gitlab_enabled else 'disabled (host is not resolvable)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
