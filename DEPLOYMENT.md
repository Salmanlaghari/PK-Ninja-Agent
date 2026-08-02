# PK Ninja Agent — Vercel Deployment Guide

This guide provides instructions on how to configure, deploy, and verify the **PK-Ninja-Agent** FastAPI application successfully on Vercel.

---

## 1. Deployed Architecture on Vercel

On Vercel, the entire application is deployed using **Vercel's Modern Python Runtime**.
- The **FastAPI backend** serves as a single Serverless Function, scaling on-demand.
- The **Frontend (HTML/CSS/JS)** is served statically and dynamically through the same FastAPI instance (e.g., routing static files via `/static` and `/` index routes), eliminating CORS issues and keeping routing unified under a single domain.

---

## 2. Configuration Files

The repository is configured using Vercel's recommended zero-configuration standard.

### 2.1 `pyproject.toml`
Located in the root of the repository, this file configures the application entrypoint:
```toml
[tool.vercel]
entrypoint = "backend.main:app"
```
This tells Vercel's builder to load the `app` instance from `backend/main.py`.

### 2.2 `.python-version`
Located in the root of the repository, this file pins the Python version to prevent runtime and build-time mismatches:
```text
3.12
```

---

## 3. Serverless Filesystem & SQLite on Vercel (CRITICAL)

Vercel Functions run in a stateless, read-only serverless environment. The only writable directory is `/tmp`.

To prevent "Read-only file system" errors during database init, repository indexing, or workspace creation, you **MUST** configure the following environment variables in your Vercel Project Settings:

1. **`DATABASE_PATH`**: Set this to `/tmp/pk_ninja.db` so the SQLite database can be successfully created, migrated, and written to.
2. **`WORKSPACE_ROOT`**: Set this to `/tmp/workspaces` so the agent can safely clone, edit, and run terminal commands within a writable environment.

*Note: Since `/tmp` is ephemeral and shared across requests but not persisted long-term, task histories and workspaces will persist for the lifespan of the serverless function instance. For persistent task queues in a production serverless setup, consider using an external database or persistent volume.*

---

## 4. Environment Variables Reference

Configure these environment variables in your **Vercel Project Settings (Settings -> Environment Variables)**:

| Variable Name | Required | Default Value | Description |
|---|---|---|---|
| `DATABASE_PATH` | **Yes (on Vercel)** | `/tmp/pk_ninja.db` | Path to the SQLite database (Must be in `/tmp` for Vercel write access). |
| `WORKSPACE_ROOT` | **Yes (on Vercel)** | `/tmp/workspaces` | Path to the workspaces folder (Must be in `/tmp` for Vercel write access). |
| `GITHUB_TOKEN` | Optional | | Personal Access Token with `repo` scope to enable GitHub integration. |
| `GITHUB_OWNER` | Optional | | Your GitHub username or organization name. |
| `GITHUB_REPO` | Optional | | Your GitHub repository name. |
| `AI_PROVIDER` | Optional | `local` | Pluggable AI Provider (`local`, `openai`, `gemini`, `anthropic`, `jules`). |
| `AI_API_KEY` | Optional | | API key for the selected AI Provider. |
| `AI_MODEL` | Optional | | Model name to send to the provider (e.g., `gpt-4o-mini`, `gemini-1.5-flash`). |
| `AI_BASE_URL` | Optional | | Custom API endpoint for OpenAI-compatible providers. |
| `COMMAND_TIMEOUT_SECONDS` | Optional | `30` | Maximum time allowed for sandbox command executions. |

---

## 5. How to Deploy to Vercel

### Step 1: Push your changes to GitHub
Ensure the deployment configuration files (`pyproject.toml` and `.python-version`) are pushed to your GitHub repository.

### Step 2: Import Project on Vercel
1. Go to the [Vercel Dashboard](https://vercel.com).
2. Click **Add New -> Project**.
3. Select your repository and click **Import**.

### Step 3: Configure Project Settings
- **Framework Preset**: Select **Other** (Vercel will auto-detect the Python/FastAPI environment).
- **Build and Output Settings**: Leave empty (Vercel handles dependency installation and deployment automatically using `requirements.txt`).
- **Environment Variables**: Add `DATABASE_PATH` as `/tmp/pk_ninja.db` and `WORKSPACE_ROOT` as `/tmp/workspaces` alongside your optional API keys and tokens.

### Step 4: Deploy
Click **Deploy**. Vercel will build the environment, install the required packages, and host your PK-Ninja-Agent workspace IDE under a secure, single SSL-enabled domain.

---

## 6. Verifying Your Deployment

Once Vercel has successfully completed the deployment:
1. Open the deployment URL.
2. Visit `/health` to verify the backend is online and responding.
3. Access `/` to load the full PK Ninja Agent Workspace UI.
4. Check that static files (`/static/app.js`, `/static/style.css`) load successfully.
