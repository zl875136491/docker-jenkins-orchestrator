# External Test Environment

`auth.txt` is an operator secret file and must remain outside Git. The
repository includes a dedicated Compose topology that uses:

- the MongoDB test server from `auth.txt`, with a generated isolated database;
- a local, password-protected Redis container for Celery;
- the Jenkins and Harbor endpoints from the existing live test network;
- the local Docker Engine socket for Swarm deployment checks.

Prepare and start it from the repository root:

```bash
python3 scripts/prepare_test_environment.py
docker compose --env-file /opt/orchestrator-test/.env \
  -f docker-compose.test.yml up -d --build
```

The script writes only `/opt/orchestrator-test/.env` (mode `0600`), Redis and
Celery Beat data, and a small non-secret `environment.json`. It generates the
worker credentials, JWT secret, Fernet key, Redis password, and Mongo database
name for each run. The generated database is never the default `orchestrator`
database.

Check the environment:

```bash
docker compose --env-file /opt/orchestrator-test/.env \
  -f docker-compose.test.yml ps
curl http://127.0.0.1:18080/healthz
```

Open `http://127.0.0.1:18080/ui/` and use the generated worker credentials from
`/opt/orchestrator-test/.env`. Do not paste that file into tickets or commit it.

Stop the local containers without removing the external Mongo database:

```bash
docker compose --env-file /opt/orchestrator-test/.env \
  -f docker-compose.test.yml down
```

The test Jenkins job can be overridden with `--jenkins-job`; otherwise the
pre-existing `apps-orchestrator/live-e2e-evidence-1789368954` job is selected
when available. GitLab ref validation is enabled only when its hostname is
resolvable from the test host; otherwise Jenkins remains responsible for the
repository checkout and the test environment starts with GitLab validation
disabled.
