# Plane user-role output

The user role submitted `user-compose.yml` after reviewing the upstream
development Compose. The output:

- converted every local `build` context into an image reference produced by
  the external Jenkins build;
- removed `container_name` and `env_file`, expanding service connection
  settings into `environment`;
- kept the web, admin, space, API, worker, beat, migrator, live, proxy,
  PostgreSQL, Valkey, RabbitMQ, and MinIO service graph;
- replaced local-only persistence assumptions with named volumes and exposed
  the proxy on a Swarm-published port;
- supplied the resulting YAML as the Compose field of the user-app before
  requesting a build.

System feedback handled by the user role: a final artifact that still
contains `build` is rejected before any Swarm network or service is created.
