# docker-jenkins-orchestrator
The orchestrator of docker and jenkins jobs for apps build pipeline.

Route quality gates are documented in [路由静态检查与动态自愈](docs/route-checks.md).
New builds require an associated Git commit and `require_route` with a successful static check.
After deployment, HTTP/gRPC checks must pass before delivery succeeds; failed checks trigger build-scoped teardown.
The worker image includes Codex CLI; configure its authentication before enabling these checks.

## Local development

```bash
pip install -r requirements-dev.txt
pytest -q
uvicorn main:app --reload
```

The default settings use an in-memory repository and dispatcher, so the API can be exercised without MongoDB, Celery, Jenkins, Harbor, or Docker Services. The test suite uses fake external adapters and never creates real Jenkins builds or Harbor pushes. Set the `ORCHESTRATOR_*` variables from `.env.example` for deployment; never commit service credentials.

The dependency-free control console is served by the API at [`/ui/`](http://127.0.0.1:8000/ui/). Start the local API with `uvicorn main:app --reload`, open that URL, and use the configured client credentials to obtain OAuth2 tokens. The console covers app management, paginated build history and build detail inspection, event, image, service, service-access, alert, template, and base-image endpoints; it does not add a separate API contract. User Compose is required to use local build mode: every service must declare `build.context` and `build.dockerfile`, and that Dockerfile must exist inside the build context. Image-only services are rejected. On the **运行资源** page, choose **访问入口** to see published ports and clickable URLs. A deployed service with `endpoint: null` has no externally published port; add a port such as `18082:3000` to the final Compose artifact and rebuild it.

New builds own independent services, containers, networks, ports, private volumes, immutable workspace inputs, and image references. The **构建资源清理** page queries or asynchronously destroys one build using only its `build_id`; it preserves APP metadata, uploaded staging files, and build history. See [the API contract](docs/api-reference.md#构建隔离与资源清理) and [the implementation plan](docs/build-isolation-and-cleanup-plan.md). Historical APP-scoped resources remain protected. Cleanup never runs global prune, removes shared builder cache, or claims Harbor blob storage is freed before GC.

`ORCHESTRATOR_CLEANUP_TIMEOUT_SECONDS` defaults to 120. Optional `ORCHESTRATOR_CLEANUP_DOCKER_TARGETS` is a JSON mapping from recorded `docker:<daemon-ID>` / `docker-node:<node-ID>` identities to controlled endpoints. Missing or unreachable execution targets produce partial failure, not false success. Coordinate API, Worker, Beat, and Jenkins pipeline upgrades; do not deploy or run destructive live checks without approval.

For the container topology, copy `.env.example` to `.env`, supply every required secret, and validate the rendered deployment before starting it:

```bash
docker compose config -q
docker compose up --build
```

The production topology runs FastAPI, a Celery worker, Celery beat, MongoDB, and Redis. Redis is only the Celery broker; MongoDB persists application state, build lifecycle records, events, images, deployment services, and alerts. The worker requires Docker Engine access only when it deploys Swarm services or synchronizes base images. In the external test environment, the same production settings are used with real MongoDB, Redis/Celery, Jenkins, Harbor, and Docker Swarm; the worker waits for running tasks and probes published TCP ports before reporting success. See [docs/test-environment.md](docs/test-environment.md) and [docs/live-delivery-audit.md](docs/live-delivery-audit.md).

For the isolated deployment at `10.32.12.111:/opt/orchestrator`, see [部署与依赖记录](docs/remote-deployment.md) for service URLs, runtime versions, build mirror configuration, acceptance checks, and external credentials still required.

Before requesting a build, conductor must store a Compose document with a `services` mapping. When GitLab is configured, app creation can discover a conventional Compose file from the saved Git ref if the request omits `compose`; `POST /api/v1/jenkins/app_sync/{app_id}` refreshes the saved branch's Compose and commit metadata. Review imported files for environment-specific paths, `env_file`, build contexts, and secrets before building; discovery does not rewrite project-specific deployment values.

Compose discovery always performs a full Git clone before searching the local checkout, pinned to the resolved commit. It does not use shallow/partial clones or GitLab tree/file API fallbacks. The runtime image installs Git and CA certificates; both the API and workers receive GitLab configuration. Missing Git, incomplete clones, and clone timeouts fail explicitly without overwriting a saved Compose. `ORCHESTRATOR_GIT_CLONE_TIMEOUT_SECONDS` defaults to 600 seconds.

The console's **应用配置** page supports the same workflow: leave Compose blank when creating an app to import it from GitLab, or use **同步仓库 Compose** on an existing app. It displays the saved Compose file and commit, both synchronization change flags, and the actual commit reported by a build. Sync requires confirmation, uses the app's saved repository/ref, and never starts a build automatically. See [控制台仓库同步操作说明](docs/webui-repository-sync.md).

Use the console's **文件管理** page to complete an app's workspace before building: upload multiple files or a directory, optionally set a relative directory prefix, filter and paginate the file list, or delete individual staging files after confirmation. File changes do not modify existing build snapshots or running services; upload the missing sources and create a new build to address `Workspace mount source is empty`. See [Workspace 文件管理操作说明](docs/workspace.md#控制台文件管理).

Platform timestamp fields in API responses use ISO 8601 with the East Asia UTC+08:00 offset, including historical records read from MongoDB. The console displays dates and refresh times in `Asia/Shanghai` regardless of the browser's local timezone. Database storage, internal comparisons, and JWT expiry calculations remain in UTC; no historical-data migration is required. See [API 时间输出约定](docs/api-reference.md#时间输出).

See [docs/architecture.md](docs/architecture.md) for the system boundaries, API contract, template catalog, task lifecycle, recovery behavior, and feature acceptance criteria.

接口与调用顺序：

- [路由检查、角色配置、一次自愈与失败清理](docs/route-checks.md)
- [API Reference](docs/api-reference.md)
- [Workspace 上传、构建与挂载](docs/workspace.md)
- [应用访问入口 API](docs/app-access.md)
- [API Call Flow](docs/api-call-flow.md)
- 运行中的机器可读说明：`GET /api/v1/system-guide`
- 完整 Markdown 使用指引：`GET /api/v1/readme`
- Compose 设计 Markdown 提示词：`GET /api/v1/docker/compose_prompt`
- 技术栈模板 CRUD：`/api/v1/docker/tech_stack_list`、`tech_stack_create`、`tech_stack_info/{tech_stack_id}`
- Compose 组合支持 `POST /api/v1/docker/template_compose?format=json`（JSON + 路径注释）或 `?format=yaml`（带注释 YAML）；部署 worker 会检查并管理外部 published 端口，实际端口以访问入口接口为准。
- 提示词正文：[Compose Generation Prompt](docs/compose-generation-prompt.md)

See [docs/delivery-audit.md](docs/delivery-audit.md) for the eight-project complex Compose delivery test and the remaining production integration boundaries.
