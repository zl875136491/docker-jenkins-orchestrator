# Paperless-ngx user-role output

The user role submitted `user-compose.yml` after reviewing the upstream
PostgreSQL/Tika variant. The output:

- removed `env_file` and expanded Paperless, PostgreSQL, Redis, Tika, and
  Gotenberg settings into `environment`;
- replaced relative `./export` and `./consume` host paths with named volumes;
- retained the broker, database, webserver, Gotenberg, and Tika services,
  dependencies, healthchecks, and port 8000;
- selected an explicit Paperless image tag instead of `latest`;
- supplied the resulting YAML as the Compose field of the user-app before
  requesting a build.
