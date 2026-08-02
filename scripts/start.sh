#!/usr/bin/env bash
# ── PK Ninja Agent — Production Startup Script ──────────────────────────
# Validates environment, runs checks, and starts the server.
#
# Usage:
#   ./scripts/start.sh                    # start with defaults
#   APP_ENV=production ./scripts/start.sh # production mode
# ────────────────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()   { echo -e "${GREEN}[PK-Ninja]${NC} $*"; }
warn()  { echo -e "${YELLOW}[PK-Ninja]${NC} $*"; }
error() { echo -e "${RED}[PK-Ninja]${NC} $*" >&2; }

# ── Environment defaults ────────────────────────────────────────────────
export APP_ENV="${APP_ENV:-development}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8000}"
export DATABASE_PATH="${DATABASE_PATH:-./pk_ninja.db}"
export WORKSPACE_ROOT="${WORKSPACE_ROOT:-./workspaces}"

# ── Validation ──────────────────────────────────────────────────────────
log "Starting PK Ninja Agent (env=${APP_ENV})"

# Check Python version
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]); then
    error "Python 3.10+ required, found $PY_VERSION"
    exit 1
fi
log "Python version: $PY_VERSION ✓"

# Check required packages
python3 -c "import fastapi, uvicorn, aiosqlite" 2>/dev/null || {
    error "Missing dependencies. Run: pip install -r requirements.txt"
    exit 1
}
log "Dependencies loaded ✓"

# Production safety checks
if [ "$APP_ENV" = "production" ]; then
    WARNINGS=0

    if [ "${DEBUG:-false}" = "true" ]; then
        warn "DEBUG=true is not recommended in production"
        WARNINGS=$((WARNINGS + 1))
    fi

    if [ "${AUTH_ENABLED:-false}" = "false" ]; then
        warn "AUTH_ENABLED=false — dashboard is unprotected"
        WARNINGS=$((WARNINGS + 1))
    fi

    if [ -z "${AUTH_SECRET:-}" ]; then
        warn "AUTH_SECRET is empty — sessions cannot be signed securely"
        WARNINGS=$((WARNINGS + 1))
    fi

    if [ $WARNINGS -gt 0 ]; then
        warn "$WARNINGS production safety warning(s)"
    else
        log "Production safety checks passed ✓"
    fi
fi

# Create runtime directories
mkdir -p "$(dirname "$DATABASE_PATH")" "$WORKSPACE_ROOT" "${LOG_DIR:-./logs}"
log "Runtime directories ready ✓"

# ── Database migration ──────────────────────────────────────────────────
log "Running database migrations..."
python3 -c "
import asyncio, sys
sys.path.insert(0, '.')
from backend.main import init_db
asyncio.run(init_db())
print('Database schema ensured')
" || {
    error "Database migration failed"
    exit 1
}
log "Database ready ✓"

# ── Start server ────────────────────────────────────────────────────────
WORKERS="${UVICORN_WORKERS:-1}"
LOG_LEVEL="${LOG_LEVEL:-info}"

log "Starting uvicorn on ${HOST}:${PORT} (workers=${WORKERS})"
exec python3 -m uvicorn backend.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --workers "$WORKERS" \
    --log-level "$LOG_LEVEL" \
    --access-log \
    --proxy-headers \
    --forwarded-allow-ips='*'
