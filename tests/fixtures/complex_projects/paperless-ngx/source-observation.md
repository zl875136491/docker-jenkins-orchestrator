# Paperless-ngx source observation

The upstream PostgreSQL/Tika variant defines Valkey, PostgreSQL, the
Paperless webserver, Gotenberg, and Tika. The webserver consumes
`docker-compose.env`, uses relative host bind paths for export/consume, and
the source file leaves image and runtime configuration choices to Compose
environment handling.

The user role expanded the required service environment, changed relative
bind paths to named volumes suitable for a Swarm deployment, retained the
PostgreSQL/Tika dependency graph, and selected explicit image tags. The
resulting YAML is the input used by the generic delivery test.
