# docker-jenkins-orchestrator
The orchestrator of docker and jenkins jobs for apps build pipeline.

## Local development

```bash
pip install -r requirements.txt
pytest -q
uvicorn main:app --reload
```

The default settings use an in-memory repository and dispatcher, so the API can be exercised without MongoDB, Celery, Jenkins, Harbor, or Docker Services. The test suite uses fake external adapters and never creates real Jenkins builds or Harbor pushes. Set the `ORCHESTRATOR_*` variables from `.env.example` for deployment; never commit service credentials.

The dependency-free control console is served by the API at [`/ui/`](http://127.0.0.1:8000/ui/). Start the local API with `uvicorn main:app --reload`, open that URL, and use the configured worker credentials to obtain a JWT. The console covers app management, paginated build history and build detail inspection, event, image, service, service-access, alert, template, and base-image endpoints; it does not add a separate API contract. On the **运行资源** page, choose **访问入口** to see published ports and clickable URLs. A deployed service with `endpoint: null` has no externally published port; add a port such as `18082:3000` to the final Compose artifact and rebuild it.

For the container topology, copy `.env.example` to `.env`, supply every required secret, and validate the rendered deployment before starting it:

```bash
docker compose config -q
docker compose up --build
```

The production topology runs FastAPI, a Celery worker, Celery beat, MongoDB, and Redis. Redis is only the Celery broker; MongoDB persists application state, build lifecycle records, events, images, deployment services, and alerts. The worker requires Docker Engine access only when it deploys Swarm services or synchronizes base images. In the external test environment, the same production settings are used with real MongoDB, Redis/Celery, Jenkins, Harbor, and Docker Swarm; the worker waits for running tasks and probes published TCP ports before reporting success. See [docs/test-environment.md](docs/test-environment.md) and [docs/live-delivery-audit.md](docs/live-delivery-audit.md).

Before requesting a build, conductor must store a user-provided Compose document with a `services` mapping. If an upstream repository has no suitable Compose file, the user role must adapt a generic template outside this service and submit the resulting YAML; the orchestrator reports the missing or unsupported input instead of guessing it.

See [docs/architecture.md](docs/architecture.md) for the system boundaries, API contract, template catalog, task lifecycle, recovery behavior, and feature acceptance criteria.

接口与调用顺序：

- [API Reference](docs/api-reference.md)
- [API Call Flow](docs/api-call-flow.md)
- 运行中的机器可读说明：`GET /api/system-guide`

See [docs/delivery-audit.md](docs/delivery-audit.md) for the eight-project complex Compose delivery test and the remaining production integration boundaries.
