# Immich user-role output

The user role submitted `user-compose.yml` after reviewing the upstream
template. The output:

- removed `container_name`, `env_file`, and optional `extends` hardware
  overlays;
- resolved database, Redis, upload, and cache settings into `environment`;
- replaced `${UPLOAD_LOCATION}` and `${DB_DATA_LOCATION}` with explicit
  deployment paths;
- retained four services, digest-compatible image references, healthchecks,
  the 2283 endpoint, named/bind volumes, and dependencies;
- supplied the resulting YAML as the Compose field of the user-app before
  requesting a build.
