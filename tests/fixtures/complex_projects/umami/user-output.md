# Umami user-role output

The user role submitted `user-compose.yml` after reviewing the pinned
upstream template. The output:

- removed the unsupported Compose `init` flag;
- expanded the database URL and application secrets into `environment`;
- renamed the database service reference consistently and kept the
  `service_healthy` dependency;
- retained the web/database healthchecks, named persistence volume, and port
  3000;
- supplied the resulting YAML as the user-app Compose input before requesting
  a build.

System feedback handled by the user role: an app registered without Compose
cannot create a build, and a final artifact containing `init` is rejected by
the Swarm contract before network creation.
