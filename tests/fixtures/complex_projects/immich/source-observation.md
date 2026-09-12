# Immich source observation

The upstream `docker/docker-compose.yml` defines four services: server,
machine-learning, Valkey, and PostgreSQL. The upstream file uses
`container_name`, `.env` through `env_file`, variable interpolation for upload
and database paths, and `shm_size`. It also documents optional hardware
acceleration overlays through `extends`.

Those values are valid Docker Compose concerns but are not portable into the
orchestrator's Swarm service contract. The user role therefore produced
`user-compose.yml` with explicit environment values, app-scoped bind/named
volumes, image references, healthchecks, and service dependencies. Hardware
acceleration is intentionally omitted until a separate Swarm device/resource
contract exists.
