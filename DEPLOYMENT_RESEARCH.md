# PK Ninja Agent — Free Deployment Platform Research (August 2026)

## Comparison Table

| Feature | Render | Fly.io | Railway | Koyeb | Glitch | Vercel | Netlify |
|---------|--------|--------|---------|-------|--------|--------|---------|
| **Free Hosting** | ✅ 750h/month | ⚠️ $5 trial | ⚠️ $5 credit | ⚠️ Credit card | ✅ 1000h/month | ❌ Serverless only | ❌ Static only |
| **Free Subdomain** | `.onrender.com` | `.fly.dev` | `.up.railway.app` | `.koyeb.app` | `.glitch.me` | `.vercel.app` | `.netlify.app` |
| **Custom Domain** | ✅ Free | ✅ Free | ✅ Free | ✅ Free | ✅ Free | ✅ Free | ✅ Free |
| **Docker Support** | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| **Python/FastAPI** | ✅ | ✅ | ✅ | ✅ | ⚠️ Limited | ❌ Serverless | ❌ Static |
| **GitHub Auto Deploy** | ✅ | ✅ (Actions) | ✅ | ✅ | ✅ | ✅ | ✅ |
| **HTTPS/SSL** | ✅ Auto | ✅ Auto | ✅ Auto | ✅ Auto | ✅ Auto | ✅ Auto | ✅ Auto |
| **Credit Card Required** | ❌ No | ✅ Yes | ✅ Yes | ✅ Yes | ❌ No | ❌ No | ❌ No |
| **Sleep/Idle Policy** | 15 min | Auto-stop | No sleep | 1 hour | 5 min | N/A | N/A |
| **CPU/RAM (Free)** | 0.1 CPU, 512MB | 1 shared CPU, 256MB | 0.5 CPU, 512MB | 0.25 CPU, 512MB | 0.1 CPU, 512MB | N/A | N/A |
| **Disk (Free)** | ❌ No persistent | 3GB persistent | 1GB | 2GB | 200MB | ❌ | ❌ |
| **Network (Free)** | 100GB/month | 160GB/month | $0.10/GB | 100GB/month | 4000 req/hour | 100GB/month | 100GB/month |
| **SQLite Support** | ⚠️ Resets on spin-down | ✅ Persistent | ✅ Persistent | ✅ Persistent | ⚠️ Limited | ❌ | ❌ |
| **WebSocket Support** | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| **SSE Support** | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| **Existing Config** | ✅ render.yaml | ✅ fly.toml | ❌ | ❌ | ❌ | ❌ | ❌ |

## Detailed Analysis

### 1. Render ⭐ RECOMMENDED
- **Pros:** No credit card, Docker support, GitHub auto-deploy, free subdomain, HTTPS, WebSocket/SSE, `render.yaml` already in repo
- **Cons:** 15-min spin-down (cold start ~30s), no persistent disk on free tier (SQLite resets), 750h/month limit
- **Best for:** PK-Ninja-Agent demo/hobby deployment
- **URL format:** `https://pk-ninja-agent.onrender.com`

### 2. Fly.io
- **Pros:** Persistent volumes (3GB), auto-stop/start, Docker support, fast cold start
- **Cons:** Credit card required, no true free tier (trial only), 256MB RAM limit
- **Best for:** Production with paid plan
- **URL format:** `https://pk-ninja-agent.fly.dev`

### 3. Railway
- **Pros:** No sleep, $5/month credit, Docker support, persistent disk
- **Cons:** Credit card required, $5 credit exhausted quickly with always-on apps
- **Best for:** Short-term projects
- **URL format:** `https://pk-ninja-agent.up.railway.app`

### 4. Koyeb
- **Pros:** 1-hour sleep (longer than Render), Docker support, persistent disk
- **Cons:** Credit card required, 1-hour inactivity timeout
- **Best for:** Apps with regular traffic
- **URL format:** `https://pk-ninja-agent.koyeb.app`

### 5. Glitch
- **Pros:** No credit card, 1000h/month, easy to use
- **Cons:** No Docker support, 5-min sleep, no WebSocket/SSE, limited Python support
- **Not suitable for:** PK-Ninja-Agent (no Docker, no WebSocket)

### 6. Vercel / Netlify
- **Pros:** No credit card, great for static sites
- **Cons:** Serverless only, no persistent backend, no Docker, no WebSocket
- **Not suitable for:** PK-Ninja-Agent (needs persistent backend)

## Recommendation

### Primary: Render

**Why Render is the best choice for PK-Ninja-Agent:**

1. **No credit card required** — truly free, no hidden charges
2. **Docker support** — uses existing Dockerfile without modification
3. **GitHub auto-deploy** — push to main, auto-deploys
4. **Free subdomain** — `pk-ninja-agent.onrender.com`
5. **HTTPS automatic** — Let's Encrypt, zero config
6. **WebSocket/SSE** — supports real-time features
7. **Existing config** — `render.yaml` already in repo
8. **One-click deploy** — Blueprint feature, no CLI needed

**Limitations (acceptable for demo/hobby):**
- 15-min spin-down → cold start ~30s on first request
- No persistent disk → SQLite resets on spin-down (fine for demo)
- 750h/month → ~25 days of continuous running

### Secondary: Fly.io (for production with persistent data)

**When to use Fly.io:**
- Need persistent SQLite data
- Need always-on service
- Willing to provide credit card
- Production workload

## PK-Ninja-Agent Specific Notes

PK-Ninja-Agent uses SQLite for persistence. On Render's free tier:
- The database resets when the service spins down (15 min inactivity)
- This is acceptable for a demo/portfolio project
- For production, use Render's paid tier ($7/mo) with persistent disk, or Fly.io

The existing `render.yaml` is correctly configured for free-tier deployment.
The `fly.toml` is correctly configured for Fly.io deployment.
