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

    # ── Provider Plugin System (v0.6.0) ─────────────────────────────────
    # Comma-separated list of provider names to enable (others are disabled).
    # Empty means "all built-in adapters enabled". Server-side only.
    provider_enabled_list: str = Field(default="", alias="PROVIDER_ENABLED")
    # Comma-separated fallback order (overrides the auto-built chain).
    # Empty means "use the auto-built chain (active first, then compatible)."
    provider_fallback_order: str = Field(default="", alias="PROVIDER_FALLBACK_ORDER")
    # When true, the agent loop uses the ProviderManager instead of the plain
    # get_provider() factory. Default false preserves existing behaviour.
    provider_manager_enabled: bool = Field(default=False, alias="PROVIDER_MANAGER_ENABLED")
    # Health-check interval in seconds (0 = disable periodic probing).
    provider_health_interval_seconds: int = Field(default=0, alias="PROVIDER_HEALTH_INTERVAL")

    # Terminal safety
    command_timeout_seconds: int = Field(default=30, alias="COMMAND_TIMEOUT_SECONDS")

    @property
    def workspace_root_path(self) -> Path:
        p = Path(self.workspace_root).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return Path(self.database_path).resolve()

    def github_repo_full(self) -> Optional[str]:
        if self.github_owner and self.github_repo:
            return f"{self.github_owner}/{self.github_repo}"
        return None

    def effective_api_key(self) -> str:
        """Resolve the API key from either the new or legacy env vars."""
        return self.ai_api_key or self.gemini_api_key

    def effective_model(self) -> str:
        """Resolve the model name from either the new or legacy env vars."""
        return self.ai_model or self.gemini_model

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
