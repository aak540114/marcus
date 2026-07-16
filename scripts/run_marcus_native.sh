#!/usr/bin/env bash
#
# scripts/run_marcus_native.sh — run Marcus natively on this host while
# Kanboard + Gitea keep running in Docker (via docker-compose.yml).
#
# Use this instead of Marcus's own container when you want Marcus's AI
# calls to use a native `claude` CLI login — most commonly because you're
# on macOS, where Claude Code's session token lives in the Keychain and
# can't be shared into a Linux container (see README.md's "Hybrid mode:
# Marcus outside Docker" section for the full explanation).
#
# Run ./scripts/setup.sh first and choose "native" for the Marcus run
# mode question — it provisions Kanboard/Gitea and writes everything this
# script needs into .env. Re-run this script any time (e.g. after a
# reboot) to start Marcus again; Kanboard/Gitea stay up independently via
# `docker compose up -d kanboard gitea`.
#
# What this script does that plain `python -m src.marcus_mcp.server --http`
# would not do for you:
#   - Points KANBOARD_URL/GITEA_URL at the Docker containers' HOST-published
#     ports (localhost:8080 / localhost:3000) instead of the internal
#     Docker service names (http://kanboard/..., http://gitea:3000) those
#     names only resolve *inside* the compose network, not on the host.
#   - Points GITEA_WEBHOOK_TARGET_URL at http://host.docker.internal:<port>
#     — the address Gitea's container uses to reach back OUT to a process
#     running on the host, the mirror image of the URL problem above.
#   - Loads scripts/marcus.native.config.json instead of the Docker image's
#     baked-in docker/marcus.docker.config.json (only the transport bind
#     host differs — see that file's comment).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env"

log() { echo "==> $*"; }
err() { echo "error: $*" >&2; }

if [ ! -f "$ENV_FILE" ]; then
    err ".env not found — run ./scripts/setup.sh first."
    exit 1
fi

env_get() {
    local key="$1"
    grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d'=' -f2- || true
}

if [ "$(env_get MARCUS_RUN_MODE)" != "native" ]; then
    err "MARCUS_RUN_MODE in .env is not 'native'."
    err "Run ./scripts/setup.sh and choose the native option, or set MARCUS_RUN_MODE=native in .env yourself."
    exit 1
fi

# ---------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------

for cmd in python3 docker; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        err "'$cmd' is required but not found on PATH."
        exit 1
    fi
done

if ! docker compose ps --status running kanboard gitea 2>/dev/null | grep -q kanboard; then
    log "WARNING: Kanboard doesn't look like it's running. Start it (and Gitea) first:"
    log "         docker compose up -d kanboard gitea"
fi

if ! python3 -c "import mcp" >/dev/null 2>&1; then
    err "Marcus's Python dependencies aren't installed in this environment."
    err "Install them, then re-run this script:"
    err "  python3 -m venv .venv && source .venv/bin/activate"
    err "  pip install -r requirements.txt && pip install --no-deps -e ."
    exit 1
fi

# ---------------------------------------------------------------------
# 2. AI provider sanity check — fail fast with a clear message instead
#    of a confusing error from deep inside Marcus's first AI call.
# ---------------------------------------------------------------------

ai_provider="$(env_get MARCUS_AI_PROVIDER)"
if [ "$ai_provider" = "claude_subscription" ]; then
    if ! command -v claude >/dev/null 2>&1; then
        err "MARCUS_AI_PROVIDER=claude_subscription but no 'claude' CLI found on PATH."
        exit 1
    fi
    if ! python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("oauthAccount") else 1)' \
            "$HOME/.claude.json" 2>/dev/null; then
        err "No authenticated 'claude' CLI login found on this host — run 'claude login' first."
        exit 1
    fi
    log "Using this host's native 'claude' CLI login (claude_subscription provider) —"
    log "this is the whole point of running natively: no Keychain-vs-container problem here."
fi

# ---------------------------------------------------------------------
# 3. Export runtime config — same values scripts/setup.sh already wrote
#    to .env for the Docker path, but pointed at the host-reachable
#    addresses instead of Docker-internal service names.
# ---------------------------------------------------------------------

export KANBOARD_URL="http://localhost:8080/jsonrpc.php"
export KANBOARD_API_TOKEN="$(env_get KANBOARD_API_TOKEN)"
export KANBOARD_PROJECT_ID="$(env_get KANBOARD_PROJECT_ID)"
export KANBOARD_WEBHOOK_TOKEN="$(env_get KANBOARD_WEBHOOK_TOKEN)"

export GITEA_URL="http://localhost:3000"
export GITEA_TOKEN="$(env_get GITEA_TOKEN)"
export GITEA_WEBHOOK_TOKEN="$(env_get GITEA_WEBHOOK_TOKEN)"
# host.docker.internal is Gitea's (a Docker container) address for
# reaching back OUT to a process on the host — the mirror image of why
# KANBOARD_URL/GITEA_URL above use localhost instead of the Docker
# service names (those only resolve the other direction, host->container).
export GITEA_WEBHOOK_TARGET_URL="http://host.docker.internal:4298/webhooks/gitea"

export MARCUS_AGENT_TOKEN="$(env_get MARCUS_AGENT_TOKEN)"
export MARCUS_AI_PROVIDER="$ai_provider"
export MARCUS_CLAUDE_CLI_MODEL="$(env_get MARCUS_CLAUDE_CLI_MODEL)"
export CLAUDE_API_KEY="$(env_get CLAUDE_API_KEY)"

# No MARCUS_HOST_PROJECT_ROOT: that variable exists only to translate
# Marcus's own in-container paths to host paths for Docker-outside-of-
# Docker (see _resolve_host_repo_path in src/core/dev_environment.py).
# A native Marcus process's paths ARE host paths already — nothing to
# translate, so this must stay unset.

# transport.http.host: unlike Docker (where the container always binds
# 0.0.0.0 internally and the HOST port-publish rule does the actual
# access control), a native process's own bind address IS what controls
# reachability — so this must mirror the MARCUS_BIND_HOST choice
# scripts/setup.sh recorded, not just default to 0.0.0.0.
bind_host="$(env_get MARCUS_BIND_HOST)"
export MARCUS_NATIVE_BIND_HOST="${bind_host:-127.0.0.1}"

export MARCUS_CONFIG="$SCRIPT_DIR/marcus.native.config.json"

# Recorded so scripts/teardown.sh can find and stop this process later.
# Written BEFORE exec, not after: exec replaces this shell with the
# python process IN PLACE (same PID, no fork) — so $$ captured here stays
# valid for the running Marcus process's entire lifetime. Deliberately no
# EXIT trap to clean this up: exec discards bash's traps along with
# everything else about the shell, so it would never fire on a normal
# Marcus shutdown anyway. teardown.sh instead checks whether the recorded
# PID is still alive and treats a stale file as harmless.
PID_FILE="$REPO_ROOT/.marcus_native.pid"
echo $$ > "$PID_FILE"

log "Starting Marcus natively (PID $$, KANBOARD_URL=$KANBOARD_URL, GITEA_URL=$GITEA_URL, bind=$MARCUS_NATIVE_BIND_HOST:4298)..."
exec python3 -m src.marcus_mcp.server --http
