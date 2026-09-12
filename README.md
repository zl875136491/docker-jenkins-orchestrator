# docker-jenkins-orchestrator
The orchestrator of docker and jenkins jobs for apps build pipeline.

## Local development

```bash
pip install -r requirements.txt
pytest -q
uvicorn main:app --reload
```

The default settings use an in-memory repository and dispatcher, so the API can be exercised without MongoDB, Celery, Jenkins, Harbor, or Docker Services. Set the `ORCHESTRATOR_*` variables from `.env.example` for deployment; never commit service credentials.

For the container topology, copy `.env.example` to `.env`, supply every required secret, and validate the rendered deployment before starting it:

```bash
docker compose config -q
docker compose up --build
```

The production topology runs FastAPI, a Celery worker, Celery beat, MongoDB, and Redis. Redis is only the Celery broker; MongoDB persists application state, build lifecycle records, events, images, deployment services, and alerts. The worker requires Docker Engine access only when it deploys Swarm services or synchronizes base images.

Before requesting a build, conductor must store a user-provided Compose document with a `services` mapping. If an upstream repository has no suitable Compose file, the user role must adapt a generic template outside this service and submit the resulting YAML; the orchestrator reports the missing or unsupported input instead of guessing it.

See [docs/architecture.md](docs/architecture.md) for the system boundaries, API contract, template catalog, task lifecycle, recovery behavior, and feature acceptance criteria.

See [docs/delivery-audit.md](docs/delivery-audit.md) for the eight-project complex Compose delivery test and the remaining production integration boundaries.
