# Deployment Guide — PK Ninja Agent v1.0.1

PK Ninja Agent is a FastAPI web application with SQLite persistence, WebSocket support, and a built-in frontend. It can be deployed to any platform that supports Docker containers.

---

## Quick Deploy Options

### Option 1: Render (Recommended — Free Tier)

1. **Push your code to GitHub**
2. **Go to [render.com](https://render.com)** → New → Blueprint
3. **Connect your GitHub repository** (`Salmanlaghari/PK-Ninja-Agent`)
4. **Render auto-detects `render.yaml`** and creates the service
5. **Click "Apply"** — deployment starts automatically
6. **Your app is live** at `https://pk-ninja-agent.onrender.com`

The `render.yaml` blueprint configures:
- Docker-based deployment
- Persistent disk for SQLite database
- Health check at `/health`
- Auto-deploy on push to `main`

**Cost:** Free tier (750 hours/month, spins down after 15 min inactivity)

### Option 2: Fly.io

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Login
fly auth login

# Launch (uses fly.toml in repo)
fly launch --copy-config

# Set secrets (if needed)
fly secrets set AI_API_KEY=your-key-here

# Deploy
fly deploy
```

**Cost:** Free tier (3 shared-cpu-1x VMs, 3GB persistent storage)

### Option 3: Docker (Any VPS)

```bash
# Clone
git clone https://github.com/Salmanlaghari/PK-Ninja-Agent.git
cd PK-Ninja-Agent

# Configure
cp .env.production .env
# Edit .env with your settings

# Build and run
docker compose up -d

# Verify
curl http://localhost:8000/health
```

**Cost:** VPS cost (DigitalOcean $6/mo, Hetzner $4/mo, etc.)

### Option 4: Docker Single Container

```bash
# Pull from GHCR
docker pull ghcr.io/salmanlaghari/pk-ninja-agent:1.0.1

# Run
docker run -d \
  --name pk-ninja \
  -p 8000:8000 \
  -v pk-data:/app/data \
  -v pk-workspaces:/app/workspaces \
  -e APP_ENV=production \
  -e AI_PROVIDER=local \
  ghcr.io/salmanlaghari/pk-ninja-agent:1.0.1
```

---

## Environment Variables

### Required

| Variable | Description | Default |
|----------|-------------|---------|
| `AI_PROVIDER` | AI provider: `local`, `openai` | `local` |
| `APP_ENV` | Environment: `production`, `development` | `development` |

### Optional — AI

| Variable | Description | Default |
|----------|-------------|---------|
| `AI_API_KEY` | API key for non-local providers | (empty) |
| `AI_MODEL` | Model name (e.g. `gpt-4o-mini`) | (empty) |
| `AI_BASE_URL` | OpenAI-compatible base URL | (empty) |

### Optional — GitHub

| Variable | Description | Default |
|----------|-------------|---------|
| `GITHUB_TOKEN` | GitHub personal access token | (empty) |
| `GITHUB_OWNER` | GitHub username or org | (empty) |
| `GITHUB_REPO` | GitHub repository name | (empty) |

### Optional — Authentication

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTH_ENABLED` | Enable authentication | `false` |
| `AUTH_SECRET` | HMAC secret for session tokens | (random) |
| `AUTH_GUEST_ALLOWED` | Allow guest sessions | `true` |
| `AUTH_GITHUB_ENABLED` | Allow GitHub login | `false` |

### Optional — Storage

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_PATH` | SQLite database path | `./pk_ninja.db` |
| `WORKSPACE_ROOT` | Workspace directory | `./workspaces` |

### Optional — Scheduler

| Variable | Description | Default |
|----------|-------------|---------|
| `SCHEDULER_ENABLED` | Enable task scheduler | `false` |
| `WORKER_MAX_CONCURRENCY` | Max concurrent tasks | `2` |

### Optional — Security

| Variable | Description | Default |
|----------|-------------|---------|
| `SECURITY_HARDENING_ENABLED` | Enable security pipeline | `false` |
| `COMMAND_TIMEOUT_SECONDS` | Command timeout | `30` |

---

## HTTPS Configuration

### Render
HTTPS is automatic — Render provides free SSL certificates for all services.

### Fly.io
HTTPS is automatic — Fly provides free SSL certificates via Let's Encrypt.

### Custom Domain (Render)
1. Go to your service → Settings → Custom Domains
2. Add your domain (e.g. `pk.yourdomain.com`)
3. Add the CNAME record to your DNS: `pk.yourdomain.com CNAME pk-ninja-agent.onrender.com`
4. SSL certificate is provisioned automatically

### Custom Domain (Fly.io)
```bash
fly certs add pk.yourdomain.com
# Add the DNS records shown in the output
```

### Nginx + Certbot (Self-hosted)
```bash
# Install certbot
apt install certbot python3-certbot-nginx

# Get certificate
certbot --nginx -d pk.yourdomain.com

# Auto-renew
certbot renew --dry-run
```

---

## Production Checklist

Before going live:

- [ ] Set `APP_ENV=production`
- [ ] Set `DEBUG=false`
- [ ] Configure `AI_PROVIDER` and `AI_API_KEY` (if using non-local provider)
- [ ] Set `AUTH_SECRET` (if enabling auth)
- [ ] Verify `/health` returns 200
- [ ] Test WebSocket connection (`/api/tasks/{id}/ws`)
- [ ] Verify static assets load (`/`, `/static/style.css`)
- [ ] Set up monitoring (uptime check on `/health`)
- [ ] Set up log aggregation
- [ ] Test backup/restore procedure
- [ ] Run `./scripts/audit.sh`

---

## Monitoring

### Health Check
```bash
curl https://your-domain.com/health
# {"status": "ok", "version": "1.0.1"}
```

### System Health (detailed)
```bash
curl https://your-domain.com/api/system/health
```

### Prometheus Metrics
```bash
# Install prometheus_client first
pip install prometheus_client

# Then scrape
curl https://your-domain.com/metrics
```

---

## Troubleshooting

### Container won't start
```bash
docker logs pk-ninja-agent
```

### Database locked
SQLite allows only one writer. Ensure only one instance writes to the DB.

### High memory usage
- Reduce `WORKER_MAX_CONCURRENCY`
- Check workspace sizes

### WebSocket not working behind proxy
Ensure your proxy forwards WebSocket upgrade headers. The `nginx.conf` in the repo includes this configuration.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│  Nginx / Reverse Proxy (TLS termination)   │
├─────────────────────────────────────────────┤
│  FastAPI (uvicorn, single worker)           │
│  ├── REST API (/api/*)                      │
│  ├── WebSocket (/api/tasks/*/ws)            │
│  ├── SSE (/api/tasks/*/stream)              │
│  ├── Static files (/static/*)               │
│  └── Health check (/health)                 │
├─────────────────────────────────────────────┤
│  SQLite (WAL mode)                          │
│  ├── tasks, events                          │
│  ├── sessions, settings                     │
│  └── repo_files, repo_symbols               │
├─────────────────────────────────────────────┤
│  Filesystem                                 │
│  ├── /app/data/ (database)                  │
│  └── /app/workspaces/ (git repos)           │
└─────────────────────────────────────────────┘
```
