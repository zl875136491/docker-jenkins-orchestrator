# Complex project delivery fixtures

The current set contains the original three reference projects plus five
additional independent open-source projects selected for this audit:
Umami, SearXNG, Open WebUI, Linkwarden, and Planka.

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

The upstream observations were made from these branch/file commits on 2026-09-12 and 2026-09-13:

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
- Umami: `umami-software/umami`, `master` at
  `ca661c7057984aa98ed4f7083d84dae2f65bfcb0`,
  `docker-compose.yml` at `a40b418038a1d3e61d152a3d3869557aecfbbce6`
- SearXNG: `searxng/searxng`, `master` at
  `56b1f64541ff6ce02dc4c8bf1aa83a799a538657`,
  `container/docker-compose.yml` at
  `6b9856d6438a546cbacbcb401334c8bdd33385b6`
- Open WebUI: `open-webui/open-webui`, `main` at
  `0a7c15832fb30b1903753e83f81dc7d27e5b0944`,
  `docker-compose.yaml` at `ca332db7eb8bbc8496a9fc771834bea257446e54`
- Linkwarden: `linkwarden/linkwarden`, `main` at
  `952ac4540657cae3a67c3ca59433899d2fda8374`,
  `docker-compose.yml` at `e0cd593b5b5b657d8db96666a0d4ccc331861fc5`
- Planka: `plankanban/planka`, `master` at
  `26b512193a6a726e4de46dcdbf21ddb1c34c365b`,
  `docker-compose.yml` at `de4d7688317829e2a0b665fe9dbf39eaf127b6f9`

No upstream source, credentials, or image is downloaded by the test suite.
