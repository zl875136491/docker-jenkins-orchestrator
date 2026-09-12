# Complex project delivery fixtures

These fixtures model the two roles used by the verification:

1. The project/system observer reads the upstream repository and records the
   original deployment shape in each `source-observation.md`.
2. The user role prepares a deployable Compose document outside the
   orchestrator, using the upstream template as a reference. That output is
   the corresponding `user-compose.yml`.

The orchestrator only receives `user-compose.yml`. It does not inspect a
repository name, generate project-specific YAML, resolve `env_file`, build
contexts, or guess missing services. The parameterized test loads every
fixture and sends it through the same `DockerSwarmAdapter` validation and fake
Docker deployment path.

The upstream observations were made from these default branches on 2026-09-12:

- Immich: `immich-app/immich`, `main`, `docker/docker-compose.yml`
- Plane: `makeplane/plane`, `preview`, `docker-compose.yml`
- Paperless-ngx: `paperless-ngx/paperless-ngx`, `dev`,
  `docker/compose/docker-compose.postgres-tika.yml`

No upstream source, credentials, or image is downloaded by the test suite.
