"""Resolve effective per-request settings for a user (v1.2.0).

This module layers the user's stored secrets and preferences on top of the
server-side ``Settings`` snapshot so the agent loop / AI provider factory /
GitHub integration see the right credentials without each caller having to
know about the secret store.

Resolution order (highest priority first) for the AI API key:
  1. Per-user stored ``ai_api_key`` (from the secret store)
  2. ``JULES_API_KEY`` / ``AI_API_KEY`` / ``GEMINI_API_KEY`` env vars
  3. The built-in default key (``BUILTIN_AI_API_KEY``)

The active *provider name* is resolved from the user's persisted settings
preference (``ai_provider``), falling back to the env ``AI_PROVIDER``.
"""
from __future__ import annotations

import logging
from typing import Any

from config import Settings

log = logging.getLogger("pk_ninja.user_settings")


def _user_id(user: Any) -> str:
    if user is None:
        return "default"
    uid = getattr(user, "user_id", None) or "anonymous"
    return uid if uid != "anonymous" else "default"


def _is_jules_compatible(provider_name: str) -> bool:
    """The built-in key is a Jules key; it only works for the jules provider."""
    return (provider_name or "").lower().strip() == "jules"


async def resolve_user_ai_key(settings: Settings, user: Any) -> str:
    """Return the effective AI API key for ``user`` (plaintext, server-side).

    Resolution order (highest priority first):
      1. Per-provider stored key (e.g. ``jules_api_key``, ``gemini_api_key``)
      2. Generic ``ai_api_key`` stored key
      3. Server env var (``JULES_API_KEY`` / ``AI_API_KEY`` / ``GEMINI_API_KEY``)
      4. Built-in default key (``BUILTIN_AI_API_KEY``)
    """
    provider_name = await resolve_user_provider(settings, user)
    try:
        from secret_store import get_secret
        # 1. Per-provider stored key (highest priority).
        provider_key_map = {
            "jules": "jules_api_key",
            "gemini": "gemini_api_key",
            "xiaomi": "mimo_api_key",
            "openai": "openai_api_key",
        }
        provider_kind = provider_key_map.get(provider_name, "")
        if provider_kind:
            stored = await get_secret(settings, user, provider_kind)
            if stored:
                return stored
        # 2. Generic ai_api_key stored key.
        stored = await get_secret(settings, user, "ai_api_key")
        if stored:
            return stored
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read stored ai_api_key: %s", exc)
    # 3. Env vars + 4. built-in default, via the existing effective_* helpers.
    if _is_jules_compatible(provider_name):
        return settings.effective_jules_key()
    return settings.effective_api_key() or settings.builtin_ai_api_key or ""


async def resolve_user_provider(settings: Settings, user: Any) -> str:
    """Return the effective provider name for ``user``."""
    try:
        from settings_store import get_settings_for_user
        prefs = await get_settings_for_user(settings, user)
        name = (prefs.get("ai_provider") or "").strip().lower()
        if name:
            return name
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read user provider pref: %s", exc)
    return (settings.ai_provider or "local").strip().lower() or "local"


async def resolve_user_github_token(settings: Settings, user: Any) -> str:
    """Return the effective GitHub token for ``user`` (server-side only)."""
    # 1. Per-user stored token.
    try:
        from secret_store import get_secret
        stored = await get_secret(settings, user, "github_token")
        if stored:
            return stored
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read stored github_token: %s", exc)
    # 2. Env var fallback.
    return getattr(settings, "github_token", "") or ""


async def build_user_settings(settings: Settings, user: Any) -> Settings:
    """Return a ``Settings`` copy with the user's key/provider injected.

    This is the single entry point used by the task-creation endpoint to
    hand the agent a fully-resolved settings object so the existing
    ``get_provider(settings)`` factory Just Works.
    """
    provider_name = await resolve_user_provider(settings, user)
    api_key = await resolve_user_ai_key(settings, user)
    github_token = await resolve_user_github_token(settings, user)

    overrides: dict = {"ai_provider": provider_name}
    # Inject the resolved key into the right env-backed field depending on
    # the provider, so effective_jules_key() / effective_api_key() pick it up.
    provider_field_map = {
        "jules": "jules_api_key",
        "gemini": "gemini_api_key",
        "xiaomi": "mimo_api_key",
        "openai": "openai_api_key",
    }
    field = provider_field_map.get(provider_name, "ai_api_key")
    overrides[field] = api_key
    if github_token:
        overrides["github_token"] = github_token

    # v1.3.0: Inject stored github_owner/github_repo so the agent can
    # auto-clone the user's repo without server env vars.
    try:
        from settings_store import get_settings_for_user
        prefs = await get_settings_for_user(settings, user)
        g_owner = (prefs.get("github_owner") or "").strip()
        g_repo = (prefs.get("github_repo") or "").strip()
        if g_owner:
            overrides["github_owner"] = g_owner
        if g_repo:
            overrides["github_repo"] = g_repo
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read github owner/repo prefs: %s", exc)

    # Also mirror the github token into os.environ so subprocesses (gh CLI,
    # git push) inherit it for the duration of the task.
    if github_token:
        import os
        os.environ["GITHUB_TOKEN"] = github_token

    try:
        return settings.model_copy(update=overrides)
    except Exception:  # noqa: BLE001
        return settings


def using_builtin_key(settings: Settings) -> bool:
    """True when the resolved Jules key is the built-in default (no user/env key)."""
    builtin = (getattr(settings, "builtin_ai_api_key", "") or "").strip()
    if not builtin:
        return False
    if settings.jules_api_key or settings.ai_api_key or settings.gemini_api_key:
        return False
    return True
