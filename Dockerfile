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
# curl   - operator debugging only (docker compose exec marcus curl ...);
#          also used below to fetch the NodeSource setup script.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Node.js + the `claude` CLI (npm package @anthropic-ai/claude-code) —
# required by the claude_subscription AI provider
# (src/ai/providers/claude_cli_provider.py), which runs Marcus's own
# decomposition/dependency-inference/effort-estimation calls through a
# non-interactive `claude -p` invocation instead of a metered Anthropic
# API key. docker-compose.yml bind-mounts the host's ~/.claude.json and
# ~/.claude/.credentials.json into this image so the CLI here is
# authenticated the same way the host's `claude login` already is.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

WORKDIR /app

# Install dependencies from requirements.txt (mirrors [project.dependencies]
# in pyproject.toml — see that file's header comment) BEFORE copying src/,
# so a source-only change doesn't invalidate this layer and force a full
# dependency re-resolve/re-download on every `docker compose up -d --build
# marcus`. Deliberately not the `embeddings` extra — pulls sentence-
# transformers/torch, unneeded for this deployment.
COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# Template config file — every value is a bare "${VAR}" placeholder,
# resolved from the container's own environment at startup by
# MarcusConfig._substitute_env_vars(). Contains no secrets, safe to bake
# into the image; see docker/marcus.docker.config.json for why this file
# is baked in rather than volume-mounted.
COPY docker/marcus.docker.config.json ./config_marcus.json

# Register the local package as editable without re-resolving dependencies
# (already installed above) — this step is cheap and safe to re-run on
# every source change.
RUN pip install --no-cache-dir --no-deps -e .

EXPOSE 4298

# Deliberately NOT the installed `marcus` console-script entry point
# (cli_main -> main() in src/marcus_mcp/server.py): that path only builds
# a bare FastMCP app and skips the custom Starlette routes this stack
# depends on (/webhooks/kanboard, /api/gate-setting, /dev-env/*,
# /project-description) — those are only registered inside server.py's
# `if __name__ == "__main__":` block. Running the module directly via -m
# triggers that block instead, so --http here still forces HTTP transport
# via the same sys.argv check, just through the code path that actually
# has the routes scripts/setup.sh provisions (e.g. the webhook it seeds).
CMD ["python", "-m", "src.marcus_mcp.server", "--http"]
