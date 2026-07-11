#!/usr/bin/env bash
#
# scripts/setup.sh — one-command first-time setup for the Marcus +
# Kanboard + Gitea stack.
#
# Provisions everything the manual README steps used to require by hand:
#   - Kanboard: app-level API token (via env var, no UI login)
#   - Kanboard: the target project + its six required columns
#   - Kanboard: the outbound webhook (instant board updates instead of
#     Marcus's 30s poll)
#   - Gitea: admin account + access token
# then builds and starts all three containers.
#
# Safe to re-run: every step checks live state before creating or
# updating anything (see README.md's "How the setup script works" for
# details). Re-running after `docker compose down` is a fast no-op pass;
# re-running after `docker compose down -v` re-provisions everything.
#
# The only thing this script cannot generate for you is a Claude API key
# — it will prompt for one interactively if `.env` doesn't already have
# CLAUDE_API_KEY set.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env"

trap 'echo "[setup.sh] failed at line $LINENO — see the error above." >&2' ERR

log()  { echo "==> $*"; }
err()  { echo "error: $*" >&2; }

# ---------------------------------------------------------------------
# .env helpers — idempotent get/set against a simple KEY=VALUE file.
# ---------------------------------------------------------------------

touch "$ENV_FILE"

env_get() {
    local key="$1"
    grep "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d'=' -f2-
}

env_set() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"
    grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
    echo "${key}=${value}" >> "$tmp"
    mv "$tmp" "$ENV_FILE"
}

# ---------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------

log "Checking prerequisites..."
for cmd in docker curl python3 openssl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        err "'$cmd' is required but not found on PATH."
        exit 1
    fi
done
if ! docker compose version >/dev/null 2>&1; then
    err "'docker compose' (v2 plugin) is required. Install Docker Desktop or the compose-plugin package."
    exit 1
fi

# ---------------------------------------------------------------------
# 2. .env bootstrap — generate anything missing, keep anything present.
# ---------------------------------------------------------------------

log "Preparing .env..."

if [ -z "$(env_get KANBOARD_API_TOKEN)" ]; then
    env_set KANBOARD_API_TOKEN "$(openssl rand -hex 32)"
fi
if [ -z "$(env_get KANBOARD_WEBHOOK_TOKEN)" ]; then
    env_set KANBOARD_WEBHOOK_TOKEN "$(openssl rand -hex 32)"
fi
if [ -z "$(env_get KANBOARD_PROJECT_NAME)" ]; then
    env_set KANBOARD_PROJECT_NAME "Marcus Project"
fi
if [ -z "$(env_get GITEA_ADMIN_PASSWORD)" ]; then
    # Fixed, predictable default — matches Kanboard's own admin/admin
    # default. This stack is intended for local/demo use, not exposed to
    # the internet. Override by setting GITEA_ADMIN_PASSWORD in .env
    # before running this script.
    env_set GITEA_ADMIN_PASSWORD "Marcus123!"
fi

if [ -z "$(env_get CLAUDE_API_KEY)" ]; then
    if [ -t 0 ]; then
        read -r -s -p "Enter your Claude API key (from https://console.anthropic.com/): " claude_key
        echo
        if [ -z "$claude_key" ]; then
            err "No API key entered."
            exit 1
        fi
        env_set CLAUDE_API_KEY "$claude_key"
    else
        err "CLAUDE_API_KEY is not set and this shell has no terminal to prompt with."
        err "Set it first, e.g.: echo 'CLAUDE_API_KEY=sk-ant-...' >> .env"
        err "then re-run ./scripts/setup.sh"
        exit 1
    fi
fi

# ---------------------------------------------------------------------
# 3. Start Kanboard + Gitea only — Marcus needs values these produce.
# ---------------------------------------------------------------------

log "Starting Kanboard and Gitea..."
docker compose up -d kanboard gitea

wait_healthy() {
    local service="$1" timeout="${2:-120}" waited=0 cid status
    log "Waiting for $service to become healthy (timeout ${timeout}s)..."
    while true; do
        cid="$(docker compose ps -q "$service")"
        status="starting"
        if [ -n "$cid" ]; then
            status="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo starting)"
        fi
        if [ "$status" = "healthy" ]; then
            log "$service is healthy."
            return 0
        fi
        if [ "$waited" -ge "$timeout" ]; then
            err "$service did not become healthy within ${timeout}s (last status: $status)"
            docker compose logs "$service" --tail=50 || true
            return 1
        fi
        sleep 2
        waited=$((waited + 2))
    done
}

wait_healthy kanboard 120
wait_healthy gitea 120

# ---------------------------------------------------------------------
# 4. Provision the Kanboard project + columns.
# ---------------------------------------------------------------------

log "Provisioning Kanboard project and columns..."
project_id="$(python3 "$SCRIPT_DIR/provision_kanboard.py" \
    --url "http://localhost:8080/jsonrpc.php" \
    --token "$(env_get KANBOARD_API_TOKEN)" \
    --project-name "$(env_get KANBOARD_PROJECT_NAME)")"
env_set KANBOARD_PROJECT_ID "$project_id"
log "Kanboard project id: $project_id"

# ---------------------------------------------------------------------
# 5. Seed the Kanboard webhook so board changes reach Marcus instantly
#    instead of on the next 30s poll. Kanboard has no JSON-RPC method or
#    env var for this setting — it's a plain key/value row in its
#    `settings` SQLite table (option='webhook_url'/'webhook_token'),
#    read fresh on every event with no caching, so this write takes
#    effect immediately with no Kanboard restart needed.
# ---------------------------------------------------------------------

log "Seeding Kanboard webhook..."
docker compose exec -T kanboard php -r '
$token = $argv[1];
$pdo = new PDO("sqlite:/var/www/app/data/db.sqlite");
$stmt = $pdo->prepare(
    "INSERT INTO settings (option, value) VALUES (?, ?) " .
    "ON CONFLICT(option) DO UPDATE SET value=excluded.value"
);
$stmt->execute(["webhook_url", "http://marcus:4298/webhooks/kanboard"]);
$stmt->execute(["webhook_token", $token]);
' -- "$(env_get KANBOARD_WEBHOOK_TOKEN)"
log "Webhook configured: http://marcus:4298/webhooks/kanboard"

# ---------------------------------------------------------------------
# 6. Gitea: admin account + access token.
# ---------------------------------------------------------------------

log "Setting up Gitea admin account..."
create_log="$(mktemp)"
if ! docker compose exec -T -u git gitea gitea admin user create \
        --username root --password "$(env_get GITEA_ADMIN_PASSWORD)" \
        --email root@example.com --admin --must-change-password=false \
        > "$create_log" 2>&1; then
    if grep -qi "already exists" "$create_log"; then
        log "Gitea admin account already exists — skipping."
    else
        err "Failed to create Gitea admin account:"
        cat "$create_log" >&2
        rm -f "$create_log"
        exit 1
    fi
fi
rm -f "$create_log"

log "Checking for a valid Gitea access token..."
gitea_token="$(env_get GITEA_TOKEN)"
token_valid="false"
if [ -n "$gitea_token" ]; then
    http_code="$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: token ${gitea_token}" \
        http://localhost:3000/api/v1/user || echo 000)"
    [ "$http_code" = "200" ] && token_valid="true"
fi

if [ "$token_valid" = "false" ]; then
    log "Generating a new Gitea access token..."
    token_name="marcus-$(date +%s)"
    gitea_token="$(docker compose exec -T -u git gitea gitea admin user generate-access-token \
        --username root --token-name "$token_name" \
        --scopes write:repository,read:user --raw | tr -d '\r\n')"
    env_set GITEA_TOKEN "$gitea_token"
    log "Gitea token generated."
else
    log "Existing Gitea token is still valid — reusing it."
fi

# ---------------------------------------------------------------------
# 7. Build and start Marcus now that .env has everything it needs.
# ---------------------------------------------------------------------

log "Building and starting Marcus..."
docker compose up -d --build marcus
wait_healthy marcus 60

# ---------------------------------------------------------------------
# 8. Summary.
# ---------------------------------------------------------------------

echo
echo "======================================================================"
echo " Setup complete."
echo "======================================================================"
echo " Kanboard:  http://localhost:8080   (admin / admin)"
echo " Gitea:     http://localhost:3000   (root / $(env_get GITEA_ADMIN_PASSWORD))"
echo " Marcus:    http://localhost:4298/mcp"
echo
echo " Kanboard project: $(env_get KANBOARD_PROJECT_NAME) (id $(env_get KANBOARD_PROJECT_ID))"
echo " Webhook:   configured — board changes reach Marcus instantly."
echo
echo " Connect an AI agent:"
echo "   claude mcp add --transport http marcus http://localhost:4298/mcp"
echo
echo " Save the Gitea admin password above if you plan to log in manually —"
echo " it won't be printed again (it's also in .env, which is git-ignored)."
echo "======================================================================"
