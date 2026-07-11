# Dockerfile — Marcus MCP server, for local docker-compose deployment.
#
# See docker-compose.yml (root) for the full Kanboard + Gitea + Marcus stack.
# For an interactive first-time setup that provisions everything Marcus
# needs (Kanboard project/columns/token, webhook, Gitea admin/token) and
# then builds and starts this image, run ./scripts/setup.sh instead of
# invoking docker compose directly.

FROM python:3.11-slim

# git    - src/integrations/gitea_manager.py shells out to `git` (subprocess)
#          for repo init and push.
# curl   - operator debugging only (docker compose exec marcus curl ...).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache the dependency layer separately from source so `docker compose
# restart marcus` after a src/ edit doesn't reinstall everything.
COPY pyproject.toml requirements.txt ./
COPY src ./src

# Template config file — every value is a bare "${VAR}" placeholder,
# resolved from the container's own environment at startup by
# MarcusConfig._substitute_env_vars(). Contains no secrets, safe to bake
# into the image; see docker/marcus.docker.config.json for why this file
# is baked in rather than volume-mounted.
COPY docker/marcus.docker.config.json ./config_marcus.json

# Core dependencies only. Deliberately NOT `pip install -e ".[embeddings]"`
# — that extra pulls sentence-transformers/torch, unneeded for this
# deployment and would roughly double the image size and build time.
RUN pip install --no-cache-dir -e .

EXPOSE 4298

# --http forces HTTP transport regardless of config_marcus.json's
# transport.type (src/marcus_mcp/server.py's main() checks this flag
# explicitly). Kanban provider selection comes from the KANBAN_PROVIDER
# environment variable set in docker-compose.yml, not a CLI flag.
CMD ["marcus", "--http"]
