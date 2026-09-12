# Linkwarden user-role output

The user role submitted `user-compose.yml` after reviewing the pinned upstream
template. The output:

- removed all `env_file` references and expanded database, search, and auth
  settings into service `environment` mappings;
- replaced `./pgdata`, `./data`, and `./meili_data` with named volumes;
- added healthchecks and explicit `service_healthy` dependencies for the
  PostgreSQL and Meilisearch prerequisites;
- retained the three-service graph and the public port 3000;
- supplied the normalized YAML as the user-app Compose input.

System feedback handled by the user role: paths in an `env_file` or relative
bind mount cannot be resolved from a Jenkins JSON artifact, so those fields
must be normalized outside the orchestrator.
