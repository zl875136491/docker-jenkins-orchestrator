/*
 * Jenkins pipeline contract for Docker-Jenkins Orchestrator.
 *
 * The control plane supplies the source repository and Compose JSON. The job
 * checks out the repository, builds or promotes every declared service image,
 * pushes app-scoped images to Harbor, and archives an artifact that preserves
 * the complete deployable service definition. In particular, ports are copied
 * instead of being reconstructed from service names.
 */
pipeline {
  agent { label 'builder' }
  options {
    skipDefaultCheckout(true)
    disableConcurrentBuilds()
    timestamps()
    timeout(time: 30, unit: 'MINUTES')
  }
  parameters {
    string(name: 'APPID', defaultValue: 'demo-app')
    string(name: 'REPOSITORY_URL', defaultValue: '')
    string(name: 'GIT_REF', defaultValue: 'main')
    string(name: 'EXPECTED_COMMIT', defaultValue: '')
    string(name: 'IMAGE_REPOSITORY', defaultValue: '10.17.158.118/apps-orchestrator/demo-app')
    string(name: 'HARBOR_REGISTRY', defaultValue: '10.17.158.118')
    text(name: 'ENVIRONMENT_JSON', defaultValue: '{}')
    text(name: 'COMPOSE_JSON', defaultValue: '{"version":"3.9","services":{}}')
  }
  stages {
    stage('Checkout') {
      steps {
        deleteDir()
        withCredentials([usernamePassword(credentialsId: 'apps-orchestrator-gitlab', usernameVariable: 'GITLAB_USER', passwordVariable: 'GITLAB_TOKEN')]) {
          sh '''#!/usr/bin/env bash
            set -Eeuo pipefail
            set +x
            unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy || true
            export NO_PROXY="${NO_PROXY:-},gitlab.1oa.com.cn"
            export no_proxy="$NO_PROXY"
            export GIT_TERMINAL_PROMPT=0
            # Image-only Compose services are complete delivery inputs. The
            # builder still pulls and promotes every image below, but does not
            # require a source checkout that may be unreachable from the
            # isolated builder network. Source builds continue through the
            # authenticated checkout path.
            if python3 - <<'PY'
import json
import os

compose = json.loads(os.environ.get("COMPOSE_JSON", "{}"))
services = compose.get("services") if isinstance(compose, dict) else None
image_only = isinstance(services, dict) and bool(services) and all(
    isinstance(service, dict) and ("build" not in service or service.get("build") is None)
    for service in services.values()
)
raise SystemExit(0 if image_only else 1)
PY
            then
              printf '{"actual_commit":null,"ref":"%s","image_only":true}\n' "$GIT_REF" > checkout.json
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
from pathlib import Path

compose = json.loads(Path("input-compose.json").read_text())
environment = json.loads(Path("input-environment.json").read_text())
if not isinstance(compose, dict) or not isinstance(compose.get("services"), dict) or not compose["services"]:
    raise SystemExit("COMPOSE_JSON must contain a non-empty services mapping")
if not isinstance(environment, dict):
    raise SystemExit("ENVIRONMENT_JSON must be a JSON object")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", os.environ["APPID"]):
    raise SystemExit("APPID contains unsupported characters")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)+", os.environ["IMAGE_REPOSITORY"]):
    raise SystemExit("IMAGE_REPOSITORY must be a registry/repository reference")
plan = []
for name, raw in compose["services"].items():
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise SystemExit(f"invalid service name: {name!r}")
    if not isinstance(raw, dict):
        raise SystemExit(f"service {name} must be an object")
    if "env_file" in raw:
        raise SystemExit(f"service {name} uses env_file; resolve it into environment before delivery")
    build = raw.get("build")
    image = raw.get("image")
    if build is None and (not isinstance(image, str) or not image.strip()):
        raise SystemExit(f"service {name} must define image or build")
    if isinstance(build, str):
        context, dockerfile = build, "Dockerfile"
    elif isinstance(build, dict):
        context, dockerfile = build.get("context", "."), build.get("dockerfile", "Dockerfile")
    elif build is None:
        context, dockerfile = None, None
    else:
        raise SystemExit(f"service {name} has invalid build definition")
    for value, label in ((context, "build context"), (dockerfile, "dockerfile")):
        if value is not None and (not isinstance(value, str) or value.startswith("/") or ".." in Path(value).parts):
            raise SystemExit(f"service {name} has unsafe {label}")
    plan.append({"name": name, "image": image, "context": context, "dockerfile": dockerfile})
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

plan = json.loads(Path("build-plan.json").read_text())
references = {}
for item in plan:
    name = item["name"]
    destination = f"{os.environ['IMAGE_REPOSITORY']}:{os.environ['BUILD_NUMBER']}-{name}"
    source = item.get("image")
    context = item.get("context")
    if context is not None:
        command = ["docker", "build", "--pull", "-t", destination]
        dockerfile = item.get("dockerfile") or "Dockerfile"
        command.extend(["-f", dockerfile, context])
        subprocess.run(command, check=True)
    else:
        subprocess.run(["docker", "pull", source], check=True)
        # An image-only Compose service is already a complete delivery input.
        # Pull it once, then promote the exact local image to the app namespace;
        # rebuilding with --pull would make a second, unnecessary registry
        # request and can fail in restricted builder networks.
        subprocess.run(["docker", "tag", source, destination], check=True)
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
for name, service in source["services"].items():
    assert artifact["compose"]["services"][name].get("ports") == service.get("ports"), name
    assert artifact["compose"]["services"][name].get("image"), name
PY
        '''
        archiveArtifacts artifacts: 'orchestrator-result.json,checkout.json,input-compose.json', fingerprint: true
      }
    }
  }
  post {
    always { deleteDir() }
  }
}
