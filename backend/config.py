"""Centralized configuration loaded from environment variables.

No secrets are ever sent to the frontend — everything here stays server-side.

AI provider configuration is fully driven by environment variables so that
any OpenAI-compatible endpoint can be plugged in (Gemini OpenAI-compat, MiMo,
OpenAI, a local Ollama server, etc.) without touching code:

    AI_PROVIDER   = local | openai   (default: local — safe offline fallback)
    AI_API_KEY    = <key>            (only required for non-local providers)
    AI_MODEL      = <model name>     (e.g. gemini-2.0-flash, gpt-4o-mini, …)
    AI_BASE_URL   = <endpoint URL>   (OpenAI-compatible /v1 base URL)

If AI_PROVIDER is "local" (or unset), the agent uses the deterministic
LocalProvider and needs no key or network — the MVP keeps working offline.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def is_serverless() -> bool:
    """Detect whether we are running on a serverless platform (e.g. Vercel).

    Vercel's Python runtime sets ``VERCEL=1`` in the build/runtime environment.
    We also treat ``AWS_LAMBDA_FUNCTION_NAME`` and a read-only current working
    directory as serverless signals so the app degrades gracefully on any
    ephemeral, read-only-filesystem host (Vercel, AWS Lambda, etc.).
    """
    if os.environ.get("VERCEL") == "1":
        return True
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return True
    if os.environ.get("PK_NINJA_SERVERLESS", "").lower() in ("1", "true", "yes"):
        return True
    # Fallback heuristic: if the CWD is not writable we are almost certainly on
    # a read-only serverless filesystem, so redirect mutable state to /tmp.
    try:
        rw = os.access(".", os.W_OK)
    except OSError:
        rw = False
    return not rw


# The only writable directory on Vercel/AWS-Lambda serverless runtimes.
_TMP_ROOT = Path(os.environ.get("TMPDIR", "/tmp"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # GitHub (server-side only)
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_owner: str = Field(default="", alias="GITHUB_OWNER")
    github_repo: str = Field(default="", alias="GITHUB_REPO")

    # Workspaces / DB
    workspace_root: str = Field(default="./workspaces", alias="WORKSPACE_ROOT")
    database_path: str = Field(default="./pk_ninja.db", alias="DATABASE_PATH")

    # Server
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    # ── AI provider (fully configurable) ────────────────────────────────
    # Which provider to use: "local" (offline, no key) or "openai" (any
    # OpenAI-compatible REST endpoint). Default is the safe offline fallback.
    ai_provider: str = Field(default="local", alias="AI_PROVIDER")
    # API key for the chosen provider. Never exposed to the frontend.
    ai_api_key: str = Field(default="", alias="AI_API_KEY")
    # Model name to send to the provider.
    ai_model: str = Field(default="", alias="AI_MODEL")
    # OpenAI-compatible base URL (must end with /v1 or similar). If empty,
    # the OpenAIProvider uses its built-in default.
    ai_base_url: str = Field(default="", alias="AI_BASE_URL")
    # Optional temperature override.
    ai_temperature: float = Field(default=0.2, alias="AI_TEMPERATURE")
    # Request timeout for AI calls (seconds).
    ai_timeout_seconds: int = Field(default=90, alias="AI_TIMEOUT_SECONDS")

    # ── Legacy Gemini env vars (kept for backward compatibility) ────────
    # If AI_PROVIDER=gemini is set, these map into the OpenAI-compatible
    # provider using Google's OpenAI-compatible endpoint.
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-1.5-flash", alias="GEMINI_MODEL")

    # ── Xiaomi MiMo (OpenAI-compatible) ──────────────────────────────
    mimo_api_key: str = Field(default="", alias="MIMO_API_KEY")

    # ── Built-in default AI API key (v1.2.0) ──────────────────────────────
    # A built-in, provider-agnostic key shipped with the app so that users
    # who do not have their own API key can still use the AI directly. The
    # built-in key is the *lowest-priority* source: any env var (JULES_API_KEY,
    # AI_API_KEY, GEMINI_API_KEY) or a per-user stored key always wins. This
    # keeps the app usable out-of-the-box while letting operators/users
    # override it at any layer. Set BUILTIN_AI_API_KEY="" to disable.
    builtin_ai_api_key: str = Field(
        default="",
        alias="BUILTIN_AI_API_KEY",
    )

    # ── Jules (Google's async coding agent) — official REST API (v1.1.0) ────
    # Dedicated API key for the official Jules API at
    # https://jules.googleapis.com/v1alpha. Auth uses the x-goog-api-key
    # header. Falls back to AI_API_KEY / GEMINI_API_KEY for convenience.
    jules_api_key: str = Field(default="", alias="JULES_API_KEY")
    # Optional override for the Jules API base URL.
    jules_base_url: str = Field(
        default="https://jules.googleapis.com/v1alpha", alias="JULES_BASE_URL"
    )
    # Polling interval (seconds) when waiting for a Jules session to reach a
    # terminal state (COMPLETED / FAILED).
    jules_poll_interval_seconds: float = Field(
        default=3.0, alias="JULES_POLL_INTERVAL"
    )
    # Maximum total seconds to poll a single session before timing out.
    jules_poll_timeout_seconds: int = Field(
        default=600, alias="JULES_POLL_TIMEOUT"
    )
    # Maximum number of HTTP retries on transient failures (429/5xx/network).
    jules_max_retries: int = Field(default=3, alias="JULES_MAX_RETRIES")

    # ── Provider Plugin System (v0.6.0) ─────────────────────────────────
    # Comma-separated list of provider names to enable (others are disabled).
    # Empty means "all built-in adapters enabled". Server-side only.
    provider_enabled_list: str = Field(default="", alias="PROVIDER_ENABLED")
    # Comma-separated fallback order (overrides the auto-built chain).
    # Empty means "use the auto-built chain (active first, then compatible)."
    provider_fallback_order: str = Field(default="", alias="PROVIDER_FALLBACK_ORDER")
    # Comma-separated list of provider names to HIDE from the frontend /api/providers
    # and /api/config responses. Hidden providers remain fully enabled in the backend
    # (can be active, can serve as fallback) — they are simply not shown in the UI.
    # This is useful for providers like Jules that should work transparently for all
    # users without appearing in the provider selection list.
    provider_hidden_list: str = Field(default="", alias="PROVIDER_HIDDEN")
    # When true, the agent loop uses the ProviderManager instead of the plain
    # get_provider() factory. Default false preserves existing behaviour.
    provider_manager_enabled: bool = Field(default=False, alias="PROVIDER_MANAGER_ENABLED")
    # Health-check interval in seconds (0 = disable periodic probing).
    provider_health_interval_seconds: int = Field(default=0, alias="PROVIDER_HEALTH_INTERVAL")

    # Terminal safety
    command_timeout_seconds: int = Field(default=30, alias="COMMAND_TIMEOUT_SECONDS")

    # ── Authentication (v0.7.0, opt-in) ─────────────────────────────────────
    # When false (default) no authentication is required — every request is
    # treated as an anonymous guest. This preserves backward compatibility.
    auth_enabled: bool = Field(default=False, alias="AUTH_ENABLED")
    # Allow guest mode (no GitHub login) when auth is enabled.
    auth_guest_allowed: bool = Field(default=True, alias="AUTH_GUEST_ALLOWED")
    # Allow "Sign in with GitHub" (token-based verification against /user).
    auth_github_enabled: bool = Field(default=False, alias="AUTH_GITHUB_ENABLED")
    # HMAC secret for signing session tokens. If empty, a random per-process
    # secret is used (sessions won't survive a restart — fine for dev/tests).
    auth_secret: str = Field(default="", alias="AUTH_SECRET")
    # Session lifetimes (seconds).
    auth_guest_ttl_seconds: int = Field(default=14400, alias="AUTH_GUEST_TTL_SECONDS")
    auth_user_ttl_seconds: int = Field(default=604800, alias="AUTH_USER_TTL_SECONDS")

    # ── User preferences / beta settings (v0.7.0) ───────────────────────────
    # Default theme: "shinobi" (dark) or "light".
    default_theme: str = Field(default="shinobi", alias="DEFAULT_THEME")
    # Auto-save edited files before running commands/tests.
    auto_save_enabled: bool = Field(default=True, alias="AUTO_SAVE_ENABLED")
    # Auto-commit after a successful task (optional, off by default).
    auto_commit_enabled: bool = Field(default=False, alias="AUTO_COMMIT_ENABLED")
    # In-app notifications for task completion / errors.
    notifications_enabled: bool = Field(default=True, alias="NOTIFICATIONS_ENABLED")

    # ── Release / deployment (v0.7.0) ───────────────────────────────────────
    # Environment label: "development" (default), "staging", "production".
    app_env: str = Field(default="development", alias="APP_ENV")
    # Debug mode (verbose logging, detailed error pages). Off in production.
    debug: bool = Field(default=False, alias="DEBUG")
    # Public site URL (for About / links). Optional.
    site_url: str = Field(default="", alias="SITE_URL")

    # ── Multi-Agent Architecture (opt-in, Phase 9) ────────────────────────────
    # When True, the agent loop delegates orchestration to the AgentCoordinator
    # (agents.coordinator) which runs the 7 specialized agents. Defaults to
    # False so the original single-agent loop remains the stable default and
    # the UI/API surface is unchanged.
    multi_agent_enabled: bool = Field(default=False, alias="MULTI_AGENT_ENABLED")

    # ── Autonomous Execution Engine (v0.8.0, opt-in) ─────────────────────
    # When True, POST /api/tasks enqueues into the TaskScheduler instead of
    # starting immediately. A background worker drains the queue. Defaults to
    # False so the existing fire-and-forget start_task() path is unchanged
    # (backward compatible).
    scheduler_enabled: bool = Field(default=False, alias="SCHEDULER_ENABLED")
    # Maximum number of tasks the worker may run concurrently (>=1).
    worker_max_concurrency: int = Field(default=2, alias="WORKER_MAX_CONCURRENCY")
    # How often (seconds) the worker loop polls the queue when idle.
    worker_poll_interval_seconds: float = Field(default=1.0, alias="WORKER_POLL_INTERVAL_SECONDS")
    # Default retry count for tasks that fail (applied by the scheduler retry).
    scheduler_default_retries: int = Field(default=1, alias="SCHEDULER_DEFAULT_RETRIES")
    # Default priority for newly enqueued tasks (higher = sooner).
    scheduler_default_priority: int = Field(default=5, alias="SCHEDULER_DEFAULT_PRIORITY")
    # On startup, attempt to recover interrupted tasks (mark them, optionally
    # resume). When false, interrupted tasks are merely detected (safe default).
    recovery_auto_resume: bool = Field(default=False, alias="RECOVERY_AUTO_RESUME")
    # When True, the v0.8.0 enhanced security pipeline (extra blocklist,
    # destructive-argument containment, workspace validation) is applied to
    # every command and workspace. Defaults to False for backward compat —
    # the existing terminal.validate_command remains the baseline guard.
    security_hardening_enabled: bool = Field(default=False, alias="SECURITY_HARDENING_ENABLED")
    # Maximum number of files validate_workspace will scan before aborting
    # (defence against pathological / zip-bomb workspaces).
    security_max_workspace_files: int = Field(default=200_000, alias="SECURITY_MAX_WORKSPACE_FILES")

    @property
    def workspace_root_path(self) -> Path:
        p = Path(self.workspace_root)
        if is_serverless():
            # The project root is read-only on Vercel; the only writable
            # directory is /tmp. Keep workspaces there so the app boots and
            # serves requests instead of crashing on mkdir().
            p = _TMP_ROOT / "pk_ninja_workspaces"
        p = p.resolve()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Last-resort: if even /tmp is unavailable, fall back to an
            # in-memory-ish path; endpoints that need disk will degrade.
            pass
        return p

    @property
    def db_path(self) -> Path:
        # Operator override: DB_PATH (or the legacy DATABASE_PATH alias) wins
        # outright so Vercel env vars are honoured verbatim.
        env = os.environ.get("DB_PATH") or os.environ.get("DATABASE_PATH")
        if env:
            p = Path(env)
        else:
            p = Path(self.database_path)
        if is_serverless():
            # SQLite "can't be used with Vercel" per the Vercel KB because the
            # filesystem is ephemeral/read-only. We still create the DB file
            # under /tmp so the app boots and stateless endpoints (/health,
            # /api/auth/status, the dashboard UI) work. Long-lived task state
            # is inherently ephemeral on serverless and should move to a
            # managed store for production, but /tmp lets the app run.
            if not env:
                p = _TMP_ROOT / "pk_ninja.db"
        return p.resolve()

    def github_repo_full(self) -> Optional[str]:
        # 1) Explicit GITHUB_OWNER + GITHUB_REPO env vars (highest priority).
        if self.github_owner and self.github_repo:
            return f"{self.github_owner}/{self.github_repo}"
        # 2) GITHUB_REPOSITORY env var in "owner/repo" format — this is what
        #    Vercel and GitHub Actions inject automatically when the project
        #    is linked to a repo. Parsing it here means the app detects the
        #    repo even when the operator only set the single standard var.
        env_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if "/" in env_repo:
            owner, _, repo = env_repo.partition("/")
            owner = owner.strip()
            repo = repo.strip()
            if owner and repo:
                # Cache into the fields so downstream code (github.py, UI)
                # sees a consistent config without re-parsing every call.
                self.github_owner = owner
                self.github_repo = repo
                return f"{owner}/{repo}"
        return None

    def effective_api_key(self) -> str:
        """Resolve the API key from either the new or legacy env vars."""
        return self.ai_api_key or self.gemini_api_key or self.mimo_api_key

    def effective_model(self) -> str:
        """Resolve the model name from either the new or legacy env vars."""
        return self.ai_model or self.gemini_model

    def effective_jules_key(self) -> str:
        """Resolve the Jules API key.

        Priority (highest first):
          1. ``JULES_API_KEY`` env var
          2. ``AI_API_KEY`` env var (generic)
          3. ``GEMINI_API_KEY`` legacy env var
          4. The built-in default key (``BUILTIN_AI_API_KEY``)

        Per-user keys stored via the secret store are layered on top of this
        by ``get_provider()`` (see ``ai_provider.py``), so a user's own key
        always wins over everything here.
        """
        if self.jules_api_key:
            return self.jules_api_key
        if self.ai_api_key:
            return self.ai_api_key
        if self.gemini_api_key:
            return self.gemini_api_key
        return self.builtin_ai_api_key or ""

    def provider_enabled_names(self) -> list:
        """Parse PROVIDER_ENABLED into a list of provider names (empty = all)."""
        if not self.provider_enabled_list.strip():
            return []
        return [n.strip() for n in self.provider_enabled_list.split(",") if n.strip()]

    def provider_fallback_names(self) -> list:
        """Parse PROVIDER_FALLBACK_ORDER into an ordered list (empty = auto)."""
        if not self.provider_fallback_order.strip():
            return []
        return [n.strip() for n in self.provider_fallback_order.split(",") if n.strip()]

    def provider_hidden_names(self) -> list:
        """Parse PROVIDER_HIDDEN into a list of provider names to hide from
        the frontend (empty = none hidden). Hidden providers stay enabled in
        the backend — they are just not surfaced in /api/providers or
        /api/config."""
        if not self.provider_hidden_list.strip():
            return []
        return [n.strip() for n in self.provider_hidden_list.split(",") if n.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Mutate os.environ from a .env file if present (so subprocesses/gh see vars too).
def load_dotenv_into_environ(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv_into_environ()
