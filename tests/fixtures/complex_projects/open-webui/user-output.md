# Open WebUI user-role output

The user role submitted `user-compose.yml` after reviewing the pinned upstream
template. The output:

- chose the published Open WebUI image and removed the local `build` context;
- removed `container_name`, `pull_policy`, `tty`, and `extra_hosts`, which are
  not portable in the Swarm service contract;
- expanded the interpolated public port and supplied a deployment-managed
  `WEBUI_SECRET_KEY`;
- retained Ollama service discovery, the UI dependency, and both named data
  volumes.

System feedback handled by the user role: the final Jenkins artifact must not
contain build contexts or host-only fields, even though the upstream Compose
uses them for local development.
