"""Centralized configuration loaded from environment variables.

No secrets are ever sent to the frontend — everything here stays server-side.
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

    # AI provider
    ai_provider: str = Field(default="local", alias="AI_PROVIDER")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-1.5-flash", alias="GEMINI_MODEL")

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
