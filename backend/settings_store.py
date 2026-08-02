"""Persistent user settings store for PK Ninja Agent (v0.7.0).

Stores non-secret user preferences (theme, AI provider, default workspace,
terminal/git preferences, auto-save, auto-commit, notifications) in a
simple SQLite key/value table, keyed by user id. When authentication is
disabled (the default), all settings are stored under the ``"default"``
user key, so the single-user local-dev experience is unchanged.

Design:
* **Backward compatible.** When no row exists for a user, the store falls
  back to the server-side config defaults (``Settings`` fields), so the
  first ``GET /api/settings`` returns sensible values without any setup.
* **Non-secret.** Only preference data is stored here — never API keys,
  tokens, or passwords. The secret-leak guard tests verify this.
* **Merge semantics.** ``get_settings_for_user`` returns config defaults
  overlaid with any persisted overrides. ``update_settings_for_user``
  applies a partial update (only the provided fields are changed).
* **Self-contained DB.** The store uses its own small SQLite connection
  helpers (it does not depend on ``main._db`` so it can be unit-tested in
  isolation). It writes to the same database path as the main app.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

import aiosqlite

log = logging.getLogger("pk_ninja.settings")

# The canonical, ordered list of preference keys (matches SettingsOut).
# v1.3.0: Added github_login, github_avatar, github_owner, github_repo —
# these are non-secret metadata persisted at GitHub-connect time so the
# agent can auto-bind to the user's repo. The actual token is in the
# encrypted secret store; these are just identity/binding hints.
PREFERENCE_KEYS = (
    "theme",
    "ai_provider",
    "default_workspace",
    "terminal_preferences",
    "git_preferences",
    "auto_save",
    "auto_commit",
    "notifications",
    "github_login",
    "github_avatar",
    "github_owner",
    "github_repo",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""


def _default_user_id(user: Any) -> str:
    """Resolve the storage key for a user (defaults to 'default')."""
    if user is None:
        return "default"
    uid = getattr(user, "user_id", None) or "anonymous"
    return uid if uid != "anonymous" else "default"


def _config_defaults(settings: Any) -> Dict[str, Any]:
    """Pull the non-secret preference defaults from the server config."""
    return {
        "theme": getattr(settings, "default_theme", "shinobi") or "shinobi",
        "ai_provider": getattr(settings, "ai_provider", "local") or "local",
        "default_workspace": "",
        "terminal_preferences": {
            "shell": getattr(settings, "terminal_shell", "bash") or "bash",
            "font_size": 13,
            "scrollback": 5000,
        },
        "git_preferences": {
            "auto_fetch": False,
            "default_branch_prefix": "feat/",
            "sign_commits": False,
        },
        "auto_save": bool(getattr(settings, "auto_save_enabled", True)),
        "auto_commit": bool(getattr(settings, "auto_commit_enabled", False)),
        "notifications": bool(getattr(settings, "notifications_enabled", True)),
        # v1.3.0: GitHub binding defaults (empty until user connects).
        "github_login": "",
        "github_avatar": "",
        "github_owner": getattr(settings, "github_owner", "") or "",
        "github_repo": getattr(settings, "github_repo", "") or "",
    }


async def _connect(db_path: Path) -> aiosqlite.Connection:
    # Centralized serverless-safe connector (WAL + busy_timeout + dir create).
    from db import connect as _db_connect
    conn = await _db_connect(db_path)
    await conn.executescript(_SCHEMA)
    await conn.commit()
    return conn


async def get_settings_for_user(settings: Any, user: Any) -> Dict[str, Any]:
    """Return the full settings dict for ``user`` (config defaults + overrides)."""
    db_path = Path(getattr(settings, "db_path", "pk_ninja.db"))
    uid = _default_user_id(user)
    defaults = _config_defaults(settings)
    try:
        conn = await _connect(db_path)
        try:
            cursor = await conn.execute(
                "SELECT key, value FROM user_settings WHERE user_id = ?", (uid,))
            rows = await cursor.fetchall()
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("settings read failed for %s: %s", uid, exc)
        return defaults
    overrides: Dict[str, Any] = {}
    for row in rows:
        key = row["key"]
        if key not in PREFERENCE_KEYS:
            continue
        try:
            overrides[key] = json.loads(row["value"])
        except Exception:  # noqa: BLE001
            overrides[key] = row["value"]
    merged = dict(defaults)
    merged.update(overrides)
    # Ensure shape consistency for nested dicts.
    for nested in ("terminal_preferences", "git_preferences"):
        if not isinstance(merged.get(nested), dict):
            merged[nested] = defaults[nested]
    return merged


async def update_settings_for_user(settings: Any, user: Any,
                                   updates: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a partial update and return the resulting full settings dict."""
    db_path = Path(getattr(settings, "db_path", "pk_ninja.db"))
    uid = _default_user_id(user)
    import datetime as _dt
    now = _dt.datetime.utcnow().isoformat()
    current = await get_settings_for_user(settings, user)
    conn = await _connect(db_path)
    try:
        for key, value in updates.items():
            if key not in PREFERENCE_KEYS:
                continue  # ignore unknown keys (defensive)
            current[key] = value
            await conn.execute(
                "INSERT INTO user_settings (user_id, key, value, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (uid, key, json.dumps(value), now),
            )
        await conn.commit()
    finally:
        await conn.close()
    return current
