from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4

import requests

from jenkins_pipeline_gate import read_runtime_env


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an isolated APP and verify uploaded build/runtime workspace files")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--api", required=True)
    parser.add_argument("--base-image", default="scratch")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    values = read_runtime_env(args.env_file)
    session = requests.Session()
    session.trust_env = False
    base = args.api.rstrip("/")
    response = session.post(f"{base}/oauth2/token", json={
        "client_id": values["ORCHESTRATOR_WORKER_NAME"], "client_secret": values["ORCHESTRATOR_WORKER_SECRET"],
    }, timeout=30)
    response.raise_for_status()
    session.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
    appid = f"WORKSPACE-VERIFY-{uuid4().hex[:12]}"
    marker = f"workspace-{uuid4().hex}"
    static = args.base_image == "scratch"
    target = "/app" if static else "/usr/share/nginx/html"
    files = [
        ("files", ("baked.txt", marker.encode())),
        ("files", ("runtime/mounted.txt", marker.encode())),
    ]
    dockerfile = f"FROM {args.base_image}\nCOPY baked.txt {target}/baked.txt\n"
    if static:
        with tempfile.TemporaryDirectory(prefix="workspace-verifier-") as temporary:
            executable = Path(temporary) / "server"
            subprocess.run(["go", "build", "-o", str(executable), str(Path(__file__).parent / "fixtures/workspace-server.go")],
                           env={**os.environ, "CGO_ENABLED": "0", "GOOS": "linux"}, check=True)
            files.append(("files", ("server", executable.read_bytes())))
        dockerfile += "COPY --chmod=0755 server /server\nENTRYPOINT [\"/server\"]\n"
    files.append(("files", ("Dockerfile", dockerfile.encode())))
    compose = {"x-orchestrator-build-source": "workspace", "services": {"web": {
        "build": {"context": ".", "dockerfile": "Dockerfile"}, "ports": ["8080" if static else "80"],
        "volumes": [f"./runtime:{target}/runtime:ro"],
    }}}
    response = session.post(f"{base}/api/v1/jenkins/app_create", json={"appid": appid, "name": "Workspace upload verification", "compose": compose}, timeout=30)
    response.raise_for_status()
    response = session.post(f"{base}/workspace/{appid}", files=files, timeout=30)
    response.raise_for_status()
    assert len(response.json()["files"]) == len(files)
    response = session.post(f"{base}/api/v1/jenkins/build_create/{appid}", json={}, timeout=30)
    response.raise_for_status()
    build_id = response.json()["build_id"]
    print(json.dumps({"appid": appid, "build_id": build_id, "phase": "queued"}), flush=True)
    response = session.post(f"{base}/workspace/{appid}", files={"files": ("runtime/mounted.txt", b"next-build-only")}, timeout=30)
    response.raise_for_status()
    deadline = time.monotonic() + args.timeout
    previous = None
    while time.monotonic() < deadline:
        response = session.get(f"{base}/api/v1/jenkins/build_info/{build_id}", timeout=30)
        response.raise_for_status()
        build = response.json()
        if build["status"] != previous:
            print(json.dumps({"build_id": build_id, "status": build["status"], "jenkins_build_number": build.get("jenkins_build_number"), "error": build.get("error")}), flush=True)
            previous = build["status"]
        if build["status"] in ("failed", "cancelled"):
            raise SystemExit("Workspace delivery failed; inspect the reported build")
        if build["status"] == "succeeded":
            break
        time.sleep(5)
    else:
        raise SystemExit("Workspace delivery timed out")
    response = session.get(f"{base}/api/v1/jenkins/app_access/{appid}", timeout=30)
    response.raise_for_status()
    access = response.json()
    urls = [url for service in access["services"] for url in service["access_urls"]]
    if not urls:
        raise SystemExit("Workspace verification service has no public URL")
    public = requests.Session()
    public.trust_env = False
    for path in ("baked.txt", "runtime/mounted.txt"):
        response = public.get(f"{urls[0].rstrip('/')}/{path}", timeout=30)
        response.raise_for_status()
        assert response.text == marker, f"Incorrect file content: {path}"
    print(json.dumps({"passed": True, "appid": appid, "build_id": build_id, "jenkins_build_number": build["jenkins_build_number"],
                      "image_baked_file": True, "whole_workspace_mount": False,
                      "runtime_mounted_file": True, "upload_after_queue_isolated": True,
                      "url": urls[0], "service_ids": build["service_ids"]}), flush=True)


if __name__ == "__main__":
    main()
