# Deployment Guide — PK Ninja Agent v0.9.0

This guide covers deploying PK Ninja Agent in production, staging, and development environments.

---

## Quick Start (Docker Compose)

The fastest way to run PK Ninja Agent in production:

```bash
# Clone the repository
git clone https://github.com/Salmanlaghari/PK-Ninja-Agent.git
cd PK-Ninja-Agent

# Configure environment
cp .env.example .env
# Edit .env with your settings (see Configuration below)

# Start with Docker Compose
docker compose up -d

# Verify
curl http://localhost:8000/health
```

The application will be available at `http://localhost:8000` with nginx reverse proxy at port 80.

---

## Docker

### Build Image

```bash
docker build -t pk-ninja-agent .
```

### Run Container

```bash
docker run -d \
  --name pk-ninja-agent \
  -p 8000:8000 \
  --env-file .env \
  -v pk-data:/app/data \
  -v pk-workspaces:/app/workspaces \
  pk-ninja-agent
```

### Health Check

The container includes a built-in health check:
```bash
docker inspect --format='{{.State.Health.Status}}' pk-ninja-agent
```

---

## Configuration

### Required Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AI_PROVIDER` | AI provider: `local`, `openai` | `local` |
| `AI_API_KEY` | API key for non-local providers | (empty) |
| `APP_ENV` | Environment: `development`, `staging`, `production` | `development` |

### Production-Specific Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTH_ENABLED` | Enable authentication | `false` |
| `AUTH_SECRET` | HMAC secret for session tokens | (random per-process) |
| `DEBUG` | Enable debug mode | `false` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `LOG_FILE` | Log file path (JSON format in production) | (stdout only) |
| `UVICORN_WORKERS` | Number of uvicorn workers | `1` |

### Autonomous Engine Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SCHEDULER_ENABLED` | Enable task scheduler | `false` |
| `WORKER_MAX_CONCURRENCY` | Max concurrent background tasks | `2` |
| `SECURITY_HARDENING_ENABLED` | Enable security pipeline | `false` |
| `RECOVERY_AUTO_RESUME` | Auto-resume interrupted tasks | `false` |

---

## Startup Script

The `scripts/start.sh` script validates the environment and starts the server:

```bash
APP_ENV=production \
AUTH_ENABLED=true \
AUTH_SECRET=your-secret-here \
./scripts/start.sh
```

The script:
1. Validates Python version (3.10+)
2. Checks required dependencies
3. Runs production safety warnings
4. Creates runtime directories
5. Runs database migrations
6. Starts uvicorn with configured options

---

## Security Audit

Run the security audit script before deploying:

```bash
./scripts/audit.sh
```

This checks:
- Dependency vulnerabilities (pip-audit)
- Security scan (bandit)
- Hardcoded secrets
- Git .env tracking
- Dangerous imports

---

## Database

PK Ninja Agent uses SQLite for all persistence. The database file is at `DATABASE_PATH` (default: `./pk_ninja.db`).

### Backup

```python
from backend.backup import BackupManager

mgr = BackupManager(db_path="./pk_ninja.db", backup_dir="./backups")
mgr.create_backup()           # Create a backup
mgr.list_backups()            # List all backups
mgr.cleanup_old_backups(keep=7)  # Keep only 7 most recent
```

Or via API (when enabled):
```bash
curl -X POST http://localhost:8000/api/backup
```

### Restore

```python
mgr.restore_backup("pk_ninja_20260802_120000.db", confirm=True)
```

---

## Monitoring

### Health Endpoint

```bash
curl http://localhost:8000/health
# {"status": "ok", "version": "0.9.0"}
```

### System Health (detailed)

```bash
curl http://localhost:8000/api/system/health
```

### Prometheus Metrics

Install `prometheus_client` to enable:
```bash
pip install prometheus_client
```

Then scrape `http://localhost:8000/metrics`.

### Structured Logging

In production (`APP_ENV=production`), logs are emitted as JSON:
```json
{"timestamp": "2026-08-02T10:00:00+00:00", "level": "INFO", "logger": "pk_ninja.access", "message": "GET /health → 200 (2ms)", "request_id": "abc123", "method": "GET", "path": "/health", "status_code": 200, "duration_ms": 2.0}
```

---

## CI/CD

### GitHub Actions

The repository includes two workflows:

**CI (`ci.yml`)** — Runs on every push/PR:
- Tests on Python 3.10, 3.11, 3.12
- Dependency audit (pip-audit)
- Security scan (bandit)
- Docker build verification
- Lint (ruff)

**Release (`release.yml`)** — Runs on version tags:
- Full test suite
- Docker image build + push to GHCR
- GitHub Release creation

### Creating a Release

```bash
git tag -a v0.9.0 -m "v0.9.0 — Production & Deployment"
git push origin v0.9.0
```

---

## Production Checklist

Before going live:

- [ ] Set `APP_ENV=production`
- [ ] Set `AUTH_ENABLED=true` and `AUTH_SECRET=<strong-secret>`
- [ ] Set `DEBUG=false`
- [ ] Configure `AI_PROVIDER` and `AI_API_KEY`
- [ ] Set up database backups (cron or API)
- [ ] Run `./scripts/audit.sh` — all checks pass
- [ ] Verify `/health` endpoint returns 200
- [ ] Set up TLS termination (nginx, cloudflare, etc.)
- [ ] Configure log aggregation (ELK, Loki, etc.)
- [ ] Set up monitoring alerts on `/metrics`
- [ ] Review and enable `SECURITY_HARDENING_ENABLED` if needed
- [ ] Test disaster recovery (backup + restore)

---

## Troubleshooting

### Container won't start
```bash
docker logs pk-ninja-agent
```

### Database locked
SQLite allows only one writer. If you see "database is locked":
- Ensure only one instance writes to the DB
- Use WAL mode (enabled by default)

### High memory usage
- Reduce `WORKER_MAX_CONCURRENCY`
- Check workspace sizes (`SECURITY_MAX_WORKSPACE_FILES`)

### Tests failing in CI
- Ensure `AI_PROVIDER=local` and `GITHUB_TOKEN=""` in CI env
- Check Python version matrix matches your code
