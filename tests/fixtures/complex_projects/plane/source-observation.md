# Plane source observation

The upstream `docker-compose.yml` contains web/admin/space/api/worker/beat,
migrator/live, PostgreSQL, Valkey, RabbitMQ, MinIO, and proxy services. The
application services use local `build` contexts, `container_name`, and
`env_file`; the file is a development-oriented source build rather than a
portable Swarm artifact.

The user role converted each build context to an image reference that the
external Jenkins job can build and publish, expanded the required environment
values, replaced host-specific bind assumptions with named volumes, and kept
the service graph. The result is deliberately a normal Compose document with
no Plane-specific adapter behavior.
