/*
 * Jenkins pipeline contract for Docker-Jenkins Orchestrator.
 *
 * The control plane supplies the source repository and Compose JSON. The job
 * checks out the repository, builds every declared service image locally,
 * pushes app-scoped images to Harbor, and archives an artifact that preserves
 * the complete deployable service definition. In particular, ports are copied
 * instead of being reconstructed from service names.
 */
pipeline {
  agent {
    node {
      label 'builder'
      customWorkspace "workspace/${env.JOB_NAME}/builds/${params.BUILD_ID ==~ /[a-f0-9]{32}/ ? params.BUILD_ID : env.BUILD_NUMBER}"
    }
  }
  environment {
    PLATFORM_BUILD_ID = "${params.BUILD_ID}"
  }
  options {
    skipDefaultCheckout(true)
    disableConcurrentBuilds()
    timestamps()
    timeout(time: 30, unit: 'MINUTES')
  }
  parameters {
    string(name: 'APPID', defaultValue: 'demo-app')
    string(name: 'BUILD_ID', defaultValue: '')
    string(name: 'REPOSITORY_URL', defaultValue: '')
    string(name: 'GIT_REF', defaultValue: 'main')
    string(name: 'EXPECTED_COMMIT', defaultValue: '')
    string(name: 'IMAGE_REPOSITORY', defaultValue: '10.17.158.118/apps-orchestrator/demo-app')
    string(name: 'HARBOR_REGISTRY', defaultValue: '10.17.158.118')
    text(name: 'ENVIRONMENT_JSON', defaultValue: '{}')
    text(name: 'COMPOSE_JSON', defaultValue: '{"version":"3.9","services":{}}')
    string(name: 'WORKSPACE_ARCHIVE_URL', defaultValue: '')
    string(name: 'WORKSPACE_ARCHIVE_SHA256', defaultValue: '')
    password(name: 'WORKSPACE_ARCHIVE_TOKEN', defaultValue: '')
    string(name: 'CHECKOUT_REQUIRED', defaultValue: 'true')
  }
  stages {
    stage('Initialize') {
      steps { deleteDir() }
    }
    stage('Checkout') {
      when {
        expression {
          params.CHECKOUT_REQUIRED != 'false'
        }
      }
      steps {
        withCredentials([usernamePassword(credentialsId: 'apps-orchestrator-gitlab', usernameVariable: 'GITLAB_USER', passwordVariable: 'GITLAB_TOKEN')]) {
          sh '''#!/usr/bin/env bash
            set -Eeuo pipefail
            set +x
            unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy || true
            export NO_PROXY="${NO_PROXY:-},gitlab.1oa.com.cn"
            export no_proxy="$NO_PROXY"
            export GIT_TERMINAL_PROMPT=0
            # Every service is a local source build. Only an explicitly
            # uploaded workspace build can skip Git checkout.
            if python3 - <<'PY'
import json
import os

compose = json.loads(os.environ.get("COMPOSE_JSON", "{}"))
workspace_source = compose.get("x-orchestrator-build-source") == "workspace"
raise SystemExit(0 if workspace_source else 1)
PY
            then
              printf '{"actual_commit":null,"ref":"%s","workspace_source":true}\n' "$GIT_REF" > checkout.json
              exit 0
            fi
            askpass="$WORKSPACE/.git-askpass"
            cat > "$askpass" <<'EOF'
#!/usr/bin/env sh
case "$1" in
  *Username*) printf '%s\\n' "$GITLAB_USER" ;;
  *) printf '%s\\n' "$GITLAB_TOKEN" ;;
esac
EOF
            chmod 700 "$askpass"
            export GIT_ASKPASS="$askpass"
            git init .
            git remote add origin "$REPOSITORY_URL"
            git -c http.proxy= -c https.proxy= fetch --depth 1 origin "$GIT_REF"
            git checkout --detach FETCH_HEAD
            actual="$(git rev-parse HEAD)"
            expected_commit="${EXPECTED_COMMIT:-}"
            if [ -n "$expected_commit" ] && [ "$actual" != "$expected_commit" ]; then
              printf 'checked out %s, expected %s\\n' "$actual" "$expected_commit" >&2
              exit 1
            fi
            rm -f "$askpass"
            printf '{"actual_commit":"%s","ref":"%s"}\\n' "$actual" "$GIT_REF" > checkout.json
          '''
        }
      }
    }
    stage('Prepare Workspace') {
      steps {
        sh '''#!/usr/bin/env bash
          set -Eeuo pipefail
          set +x
          python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
from urllib.request import Request, urlopen

compose = json.loads(os.environ["COMPOSE_JSON"])
if not Path("checkout.json").exists():
    Path("checkout.json").write_text(json.dumps({"actual_commit": None, "ref": os.environ.get("GIT_REF", "main"),
        "workspace_source": compose.get("x-orchestrator-build-source") == "workspace"}))
url = os.environ.get("WORKSPACE_ARCHIVE_URL", "")
if not url:
    if compose.get("x-orchestrator-build-source") == "workspace":
        raise SystemExit("Workspace archive URL is required for uploaded-source builds")
    raise SystemExit(0)
destination = Path(".orchestrator-build-source")
if destination.exists() or destination.is_symlink():
    raise SystemExit("Reserved build workspace path already exists in repository")
try:
    with tempfile.TemporaryFile() as archive:
        digest = hashlib.sha256()
        total = 0
        request = Request(url, headers={"Authorization": "Bearer " + os.environ.get("WORKSPACE_ARCHIVE_TOKEN", "")})
        with urlopen(request, timeout=120) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 2 * 1024 * 1024 * 1024:
                    raise ValueError("Workspace archive exceeds builder limit")
                digest.update(chunk)
                archive.write(chunk)
        if digest.hexdigest() != os.environ.get("WORKSPACE_ARCHIVE_SHA256"):
            raise ValueError("Workspace archive checksum mismatch")
        if compose.get("x-orchestrator-build-source") == "workspace":
            destination.mkdir()
        else:
            shutil.copytree(Path.cwd(), destination, symlinks=True,
                            ignore=shutil.ignore_patterns(".git", ".orchestrator-build-source"))
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode="r:") as bundle:
            for member in bundle:
                relative = PurePosixPath(member.name)
                if not member.isfile() or relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
                    raise ValueError("Unsafe workspace archive member")
                target = destination.joinpath(*relative.parts)
                for path in (target, *target.parents):
                    if path.is_symlink():
                        raise ValueError("Workspace overlay may not follow repository symlinks")
                    if path == destination:
                        break
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o644)
except Exception:
    raise SystemExit("Workspace download or extraction failed; check API reachability, token expiry, checksum and file paths") from None
PY
        '''
      }
    }
    stage('Validate Compose') {
      steps {
        sh '''#!/usr/bin/env bash
          set -Eeuo pipefail
          printf '%s' "$COMPOSE_JSON" > input-compose.json
          printf '%s' "$ENVIRONMENT_JSON" > input-environment.json
          python3 - <<'PY'
import json
import os
import re
import ipaddress
from pathlib import Path

def validate_repository(value):
    # Match Docker's repository grammar, not the looser Compose/Swarm name
    # grammar. Tags allow uppercase, but repository path components do not.
    component = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    host_component = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    registry_pattern = rf"(?:{host_component}(?:\\.{host_component})*|\\[[A-Fa-f0-9:]+\\])(?::[0-9]{{1,5}})?"
    registry, separator, path = value.partition("/")
    if not separator or not re.fullmatch(registry_pattern, registry):
        raise SystemExit("IMAGE_REPOSITORY must start with a valid registry host (and optional port)")
    if registry.startswith("["):
        try:
            ipaddress.IPv6Address(registry[1:registry.index("]")])
        except ValueError:
            raise SystemExit("IMAGE_REPOSITORY registry IPv6 address is invalid") from None
    port = registry[registry.index("]") + 1:] if registry.startswith("[") else registry.rpartition(":")[2] if ":" in registry else ""
    if port:
        digits = port.removeprefix(":")
        if len(digits) > 5 or not 1 <= int(digits) <= 65535:
            raise SystemExit("IMAGE_REPOSITORY registry port is invalid")
    if registry.lower() != "localhost" and "." not in registry and ":" not in registry:
        raise SystemExit("IMAGE_REPOSITORY registry must be localhost, a qualified host, or host:port")
    if not re.fullmatch(rf"{component}(?:/{component})*", path) or len(path) > 255:
        raise SystemExit("IMAGE_REPOSITORY repository path must be lowercase Docker components and at most 255 characters")
    if registry != os.environ["HARBOR_REGISTRY"]:
        raise SystemExit("HARBOR_REGISTRY must match the IMAGE_REPOSITORY registry")
    return value

compose = json.loads(Path("input-compose.json").read_text())
environment = json.loads(Path("input-environment.json").read_text())
if not isinstance(compose, dict) or not isinstance(compose.get("services"), dict) or not compose["services"]:
    raise SystemExit("COMPOSE_JSON must contain a non-empty services mapping")
if not isinstance(environment, dict):
    raise SystemExit("ENVIRONMENT_JSON must be a JSON object")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", os.environ["APPID"]):
    raise SystemExit("APPID contains unsupported characters")
repository = validate_repository(os.environ["IMAGE_REPOSITORY"])
if not re.fullmatch(r"[0-9]+", os.environ["BUILD_NUMBER"]):
    raise SystemExit("BUILD_NUMBER must be numeric")
build_id = os.environ.get("PLATFORM_BUILD_ID", "")
if build_id and not re.fullmatch(r"[a-f0-9]{32}", build_id):
    raise SystemExit("BUILD_ID must be a platform build identifier")
plan = []
source_root = Path.cwd().resolve()
for name, raw in compose["services"].items():
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise SystemExit(f"invalid service name: {name!r}")
    tag = f"{build_id or os.environ['BUILD_NUMBER']}-{name}"
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise SystemExit(f"image tag for service {name!r} exceeds Docker's 128-character limit")
    if not isinstance(raw, dict):
        raise SystemExit(f"service {name} must be an object")
    if "env_file" in raw:
        raise SystemExit(f"service {name} uses env_file; resolve it into environment before delivery")
    build = raw.get("build")
    image = raw.get("image")
    if not isinstance(build, dict):
        raise SystemExit(f"service {name} must use local build mode with build.context and build.dockerfile")
    context, dockerfile = build.get("context"), build.get("dockerfile")
    for value, label in ((context, "build context"), (dockerfile, "dockerfile")):
        if (not isinstance(value, str) or not value.strip() or value.startswith("/") or value.startswith("~")
                or ".." in Path(value).parts or "\\\\" in value):
            raise SystemExit(f"service {name} has unsafe or missing {label}")
    if os.environ.get("WORKSPACE_ARCHIVE_URL"):
        context = str(Path(".orchestrator-build-source") / context)
    context_path = Path(context)
    dockerfile_path = context_path / dockerfile
    try:
        if not context_path.resolve().is_relative_to(source_root) or not dockerfile_path.resolve().is_relative_to(source_root):
            raise SystemExit(f"service {name} build context or Dockerfile escapes the source root")
    except OSError:
        raise SystemExit(f"service {name} build context or Dockerfile cannot be inspected") from None
    if not context_path.is_dir():
        raise SystemExit(f"service {name} build context does not exist: {context}")
    if not dockerfile_path.is_file():
        raise SystemExit(f"service {name} Dockerfile does not exist: {dockerfile}")
    dockerfile = str(dockerfile_path)
    plan.append({"name": name, "image": image, "context": context, "dockerfile": dockerfile, "destination": f"{repository}:{tag}"})
Path("build-plan.json").write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
PY
        '''
      }
    }
    stage('Build And Push') {
      steps {
        withCredentials([usernamePassword(credentialsId: 'infra_harbor_auth', usernameVariable: 'HARBOR_USER', passwordVariable: 'HARBOR_PASSWORD')]) {
          sh '''#!/usr/bin/env bash
            set -Eeuo pipefail
            set +x
            export ORCHESTRATOR_DOCKER_PROXY_CONFIG="${DOCKER_CONFIG:-$HOME/.docker}/config.json"
            docker_config="$(mktemp -d)"
            cleanup() {
              docker --config "$docker_config" logout "$HARBOR_REGISTRY" >/dev/null 2>&1 || true
              rm -rf "$docker_config"
            }
            trap cleanup EXIT HUP INT TERM
            printf '%s' "$HARBOR_PASSWORD" | docker --config "$docker_config" login "$HARBOR_REGISTRY" --username "$HARBOR_USER" --password-stdin >/dev/null
            export DOCKER_CONFIG="$docker_config"
            python3 - <<'PY'
import json
import os
import subprocess
from pathlib import Path

proxy_source = os.environ.get("ORCHESTRATOR_DOCKER_PROXY_CONFIG")
private_directory = os.environ.get("DOCKER_CONFIG")
if proxy_source and private_directory and Path(proxy_source).is_file():
    proxies = json.loads(Path(proxy_source).read_text(encoding="utf-8")).get("proxies")
    if proxies is not None:
        if not isinstance(proxies, dict):
            raise ValueError("Docker client proxies configuration must be an object")
        private_config = Path(private_directory) / "config.json"
        settings = json.loads(private_config.read_text(encoding="utf-8")) if private_config.is_file() else {}
        settings["proxies"] = proxies
        private_config.write_text(json.dumps(settings), encoding="utf-8")
        private_config.chmod(0o600)

plan = json.loads(Path("build-plan.json").read_text())
references = {}
for item in plan:
    name = item["name"]
    destination = item["destination"]
    context = item.get("context")
    if context is None:
        raise SystemExit(f"service {name} is missing a local build context")
    command = ["docker", "build", "--pull", "-t", destination]
    dockerfile = item["dockerfile"]
    command.extend(["-f", dockerfile, context])
    subprocess.run(command, check=True)
    subprocess.run(["docker", "push", destination], check=True)
    references[name] = destination

compose = json.loads(Path("input-compose.json").read_text())
services = {}
for name, original in compose["services"].items():
    service = dict(original)
    service.pop("build", None)
    service["image"] = references[name]
    services[name] = service
result = {
    "version": compose.get("version", "3.9"),
    "images": list(references.values()),
    "compose": {"version": compose.get("version", "3.9"), "services": services},
    "source": json.loads(Path("checkout.json").read_text()),
}
if os.environ.get("PLATFORM_BUILD_ID"):
    result["build_id"] = os.environ["PLATFORM_BUILD_ID"]
    result["image_digests"] = {}
    for reference in references.values():
        inspected = subprocess.run(["docker", "image", "inspect", reference], check=True, capture_output=True, text=True)
        image = json.loads(inspected.stdout)[0]
        repository = reference.rsplit(":", 1)[0]
        matching = [digest for digest in image.get("RepoDigests", []) if digest.startswith(repository + "@")]
        if len(matching) != 1:
            raise SystemExit("Pushed build image digest could not be verified")
        result["image_digests"][reference] = matching[0].split("@", 1)[1]
if "volumes" in compose:
    # Keep explicit/external volume metadata; an alias alone cannot tell the
    # deployment adapter whether a volume is app-owned or intentionally shared.
    result["compose"]["volumes"] = compose["volumes"]
Path("orchestrator-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
PY
            test -s orchestrator-result.json
          '''
        }
      }
    }
    stage('Verify Artifact') {
      steps {
        sh '''#!/usr/bin/env bash
          set -Eeuo pipefail
          python3 - <<'PY'
import json
from pathlib import Path

source = json.loads(Path("input-compose.json").read_text())
artifact = json.loads(Path("orchestrator-result.json").read_text())
assert artifact["images"]
assert set(source["services"]) == set(artifact["compose"]["services"])
assert artifact["compose"].get("volumes") == source.get("volumes"), "top-level volumes"
for name, service in source["services"].items():
    assert artifact["compose"]["services"][name].get("ports") == service.get("ports"), name
    assert artifact["compose"]["services"][name].get("volumes") == service.get("volumes"), name
    assert artifact["compose"]["services"][name].get("image"), name
PY
        '''
        archiveArtifacts artifacts: 'orchestrator-result.json,checkout.json,input-compose.json', fingerprint: true
      }
    }
  }
  post {
    always {
      script {
        try {
          sh '''#!/usr/bin/env bash
        set -Eeuo pipefail
        python3 - <<'PY'
import json
import os
from pathlib import Path
import re
import subprocess
import shutil

build_id = os.environ.get("PLATFORM_BUILD_ID", "")
report = {"build_id": build_id, "images": [], "builder_target": None, "verification_complete": False, "workspace_removed": False, "plan_created": False}
try:
    if re.fullmatch(r"[a-f0-9]{32}", build_id):
        info = subprocess.run(["docker", "info", "--format", "{{json .ID}}"], capture_output=True, text=True)
        if info.returncode != 0:
            raise ValueError("Builder identity unavailable")
        report["builder_target"] = "docker:" + json.loads(info.stdout)
        plan_path = Path("build-plan.json")
        report["plan_created"] = plan_path.is_file()
        plan = json.loads(plan_path.read_text()) if plan_path.is_file() else []
        for item in plan:
            reference = item["destination"]
            if not reference.startswith(os.environ["IMAGE_REPOSITORY"] + ":" + build_id + "-"):
                raise ValueError("Non-build image reference")
            inspected = subprocess.run(["docker", "image", "inspect", reference], capture_output=True, text=True)
            entry = {"reference": reference, "local_removed": False, "native_id": None}
            if inspected.returncode == 0:
                entry["native_id"] = json.loads(inspected.stdout)[0]["Id"]
                removed = subprocess.run(["docker", "image", "rm", "--no-prune", reference], capture_output=True, text=True)
                entry["local_removed"] = removed.returncode == 0
            elif "no such image" in inspected.stderr.lower() or "no such object" in inspected.stderr.lower():
                entry["local_removed"] = True
            report["images"].append(entry)
        report["verification_complete"] = True
except Exception:
    report["error"] = "Jenkins builder resource verification failed"
finally:
    if re.fullmatch(r"[a-f0-9]{32}", build_id):
        try:
            root = Path.cwd()
            if root.name != build_id or root != Path(os.environ.get("WORKSPACE", "")).absolute():
                raise ValueError("Unexpected builder workspace")
            for path in root.iterdir():
                if path.is_symlink() or not path.is_dir():
                    path.unlink()
                else:
                    shutil.rmtree(path)
            report["workspace_removed"] = True
        except Exception:
            report["error"] = "Jenkins build-private workspace cleanup failed"
    Path("orchestrator-resources.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
PY
      '''
        } finally {
          try {
            archiveArtifacts artifacts: 'orchestrator-resources.json', fingerprint: true, allowEmptyArchive: true
          } finally {
            deleteDir()
          }
        }
      }
    }
  }
}
