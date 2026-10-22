FROM node:22-bookworm-slim AS codex-cli
ARG CODEX_CLI_VERSION=0.147.0
RUN npm install --global --no-audit --no-fund "@openai/codex@${CODEX_CLI_VERSION}"

FROM python:3.12-slim
ARG DEBIAN_MIRROR=https://deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /app
RUN sed -i -E "s|https?://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 install -y -qq --no-install-recommends git ca-certificates libstdc++6 \
    && git config --system advice.detachedHead false \
    && git config --system core.quotepath false \
    && rm -rf /var/lib/apt/lists/*
COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && codex --version \
    && codex exec --help >/dev/null
COPY requirements.txt .
RUN PIP_INDEX_URL="${PIP_INDEX_URL}" pip install --no-cache-dir -r requirements.txt
# Copy runtime assets explicitly: local auth.json, .codex/, ad-hoc env files,
# and diagnostic output must not become image layers via a broad COPY.
COPY main.py .
COPY orchestrator/ orchestrator/
COPY templates/ templates/
COPY docs/ docs/
COPY webui/ webui/
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
