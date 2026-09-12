# Complex project delivery fixtures

These fixtures model the two roles used by the verification:

1. The project/system observer reads the upstream repository and records the
   original deployment shape in each `source-observation.md`.
2. The user role prepares a deployable Compose document outside the
   orchestrator, using the upstream template as a reference. That output is
   the corresponding `user-compose.yml`.

The successful delivery path only receives `user-compose.yml` as its final
deployment input. It does not inspect a repository name, generate
project-specific YAML, or guess missing services. A source Compose containing
`build` or `env_file` may be passed to Jenkins as build input, but the final
`orchestrator-result.json` artifact must be normalized before the generic
`DockerSwarmAdapter` accepts it. The parameterized test loads every fixture and
sends it through the same validation and fake Docker deployment path.

The upstream observations were made from these branch/file commits on 2026-09-12:

- Immich: `immich-app/immich`, `main` at
  `7abd625af305b82a4d37b578ffc535c3d7d302dc`,
  `docker/docker-compose.yml` at `767cf230639d80c106c8dcd489397daf138569c5`
- Plane: `makeplane/plane`, `preview` at
  `2f895b82dad839c730c36a5c0cbc046f1e5d6b56`, `docker-compose.yml` at
  `787ba640aa6634916b02d2b51724d7720863c8c1`
- Paperless-ngx: `paperless-ngx/paperless-ngx`, `dev` at
  `4a54935b3dba375084c83b924bbd41ca8d04b8ac`,
  `docker/compose/docker-compose.postgres-tika.yml` at
  `136fdc5e4b6db93e6ed7492d3fae2509b40f9793`

No upstream source, credentials, or image is downloaded by the test suite.
