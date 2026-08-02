# ── PK Ninja Agent — Production Dockerfile ──────────────────────────────
# Multi-stage build for a lean, secure production image.
#
# Build:   docker build -t pk-ninja-agent .
# Run:     docker run -p 8000:8000 --env-file .env pk-ninja-agent
# Compose: docker compose up
# ────────────────────────────────────────────────────────────────────────

# ── Stage 1: Builder ────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Production ─────────────────────────────────────────────────
FROM python:3.12-slim AS production

# Security: run as non-root
RUN groupadd -r pkninja && useradd -r -g pkninja -d /app -s /sbin/nologin pkninja

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY backend/ ./backend/
COPY agents/ ./agents/
COPY providers/ ./providers/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/
COPY requirements.txt .
COPY .env.example .

# Create directories for runtime data
RUN mkdir -p /app/workspaces /app/data /app/logs && \
    chown -R pkninja:pkninja /app

# Environment defaults (overridable at runtime)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    APP_ENV=production \
    DATABASE_PATH=/app/data/pk_ninja.db \
    WORKSPACE_ROOT=/app/workspaces

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import httpx; r = httpx.get('http://localhost:8000/health'); r.raise_for_status()" || exit 1

# Expose port
EXPOSE 8000

# Switch to non-root user
USER pkninja

# Startup
CMD ["python3", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "info"]
