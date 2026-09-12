# Linkwarden source observation

The pinned upstream Compose defines PostgreSQL, Linkwarden, and Meilisearch.
All three services use environment files; PostgreSQL, Linkwarden, and
Meilisearch use relative host persistence paths or a relative data directory.
The application depends on both database and search services and publishes
port 3000.

The user role expanded the required connection and secret values, replaced
relative paths with named volumes, added explicit healthchecks for dependency
ordering, and retained the three-service application graph. The orchestrator
only receives the resulting generic Compose document.
