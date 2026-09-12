# Planka user-role output

The user role submitted `user-compose.yml` after reviewing the pinned upstream
template. The output:

- converted list-form environment values to mappings and replaced the sample
  `SECRET_KEY` with a deployment-managed secret;
- renamed the database service consistently in `DATABASE_URL` and
  `depends_on`;
- retained PostgreSQL health checking, the database/app named volumes, the
  application port 3000, and the two-service topology;
- left the optional Docker secrets and commented development features outside
  the artifact until a dedicated secrets contract is available.

System feedback handled by the user role: optional `secrets` would be rejected
by the current Swarm adapter, so the user supplied an explicit environment
value through the configured secret-injection path instead.
