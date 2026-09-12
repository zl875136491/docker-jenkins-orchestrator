# Open WebUI source observation

The pinned upstream Compose defines an Ollama model service and an Open WebUI
service. The file uses `container_name`, an optional local `build` context,
`pull_policy`, `tty`, `extra_hosts`, interpolated ports and an environment list.
The two services share a named-data pattern and the UI depends on Ollama.

The user role selected the published Open WebUI image as the Jenkins artifact,
removed local-only build and host integration fields, expanded the port and
secret values, and retained the Ollama dependency and named data volumes.
Those changes are generic Compose normalization decisions, not Open WebUI
logic in the adapter.
