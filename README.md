# docker-jenkins-orchestrator
The orchestrator of docker and jenkins jobs for apps build pipeline.

## Local development

```bash
pip install -r requirements.txt
pytest -q
uvicorn main:app --reload
```

The default settings use an in-memory repository and dispatcher, so the API can be exercised without MongoDB, Celery, Jenkins, Harbor, or Docker Services. Set the `ORCHESTRATOR_*` variables from `.env.example` for deployment; never commit service credentials.

See [docs/architecture.md](docs/architecture.md) for the system boundaries, API contract, template catalog, task lifecycle, and feature acceptance criteria.
