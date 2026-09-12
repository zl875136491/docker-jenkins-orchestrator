# Umami source observation

The pinned upstream `docker-compose.yml` defines an Umami web service and a
PostgreSQL database. The web service uses `init: true`, a published port,
database settings, a healthcheck, and a `service_healthy` dependency. The
database stores state in a named volume and also has a healthcheck.

The user role retained the application/database topology and runtime
configuration, removed the Compose-only `init` flag, replaced example secrets
with deployment-managed values, and retained explicit image references. The
result remains a normal Compose document accepted by the generic Swarm
adapter.
