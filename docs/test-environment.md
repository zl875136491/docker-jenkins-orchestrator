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
client credentials, JWT secret, Fernet key, Redis password, and Mongo database
name for each run. The generated database is never the default `orchestrator`
database.

Check the environment:

```bash
docker compose --env-file /opt/orchestrator-test/.env \
  -f docker-compose.test.yml ps
curl http://<test-host-address>:18080/healthz
```

The preparation script binds the test API/UI port to the test host's detected
outbound address, rather than only to loopback. Read the advertised URL from
`/opt/orchestrator-test/environment.json` (the current test host uses
`10.32.12.110`):

Open `http://<test-host-address>:18080/ui/` and use the generated client
credentials from `/opt/orchestrator-test/.env` with `POST /oauth2/token`. The UI uses same-origin API
requests, so this URL exercises the frontend served by the test API container.
The preparation script also writes the advertised host to
`ORCHESTRATOR_PUBLIC_HOST`, so the UI's **访问入口** view can turn published
Swarm ports into links such as `http://10.32.12.110:18082`. A service record
with `endpoint: null` and no published port is deployed but has no external
URL. A Compose mapping such as `18082:3000` requests external access; the
worker checks the configured published-port range and may assign another free
host port when `18082` is occupied. Use `published_ports` from `app_access`
as the final value.

The worker also waits for real Swarm tasks to reach `running`. When
`ORCHESTRATOR_PUBLIC_HOST` is set (as it is in this test environment), every
published TCP port is probed from the worker network before the build is marked
`succeeded`; a service that is created but not runnable or reachable is marked
failed instead.

The machine-readable access query is:

```bash
curl -H "Authorization: Bearer <token>" \
  http://<test-host-address>:18080/api/v1/jenkins/app_access/<app_id>
```

It returns one entry per deployed service, including `build_id`, `published_ports`,
`access_urls`, `access_available`, and `access_reason`. The top-level `region` field
identifies the worker region; the shared test environment uses `beijing`.
Do not paste the dotenv file into tickets or commit it. For a deliberately
loopback-only test run, pass `--api-bind-address 127.0.0.1` when preparing the
environment.

Stop the local containers without removing the external Mongo database:

```bash
docker compose --env-file /opt/orchestrator-test/.env \
  -f docker-compose.test.yml down
```

The test Jenkins job can be overridden with `--jenkins-job`; otherwise the
real `apps-orchestrator/orchestrator-real-delivery-20260920` job is selected
when available. GitLab ref validation is enabled only when its hostname is
resolvable from the test host; otherwise Jenkins remains responsible for the
repository checkout and the test environment starts with GitLab validation
disabled.
