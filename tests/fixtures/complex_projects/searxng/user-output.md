# SearXNG user-role output

The user role submitted `user-compose.yml` after reviewing the pinned
container template. The output:

- expanded the `.env`-provided secret and URL values into `environment`;
- replaced nested port interpolation with an explicit `8080:8080` mapping;
- removed `container_name` values so the orchestrator can namespace services
  by `appid`;
- replaced the relative `./core-config` bind with the absolute,
  deployment-provisioned `/srv/searxng/core-config` path;
- retained the Valkey command and both persistent cache volumes.

System feedback handled by the user role: `env_file`, fixed container names,
and relative bind mounts are not part of the Swarm artifact contract and would
fail before any Docker object is created.
