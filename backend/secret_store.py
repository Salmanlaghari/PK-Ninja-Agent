"""Per-user encrypted secret store for PK Ninja Agent (v1.2.0).

This module stores *secrets* (AI API keys, GitHub tokens) on the server side,
keyed by user id. It is deliberately **separate** from ``settings_store.py``,
which only stores non-secret preferences and has a strict secret-leak guard.

Design
------
* **Encryption at rest.** Secrets are stored as AES-256-GCM ciphertext in a
  dedicated SQLite table (``user_secrets``). The encryption key is derived
  from ``AUTH_SECRET`` (env) via PBKDF2-HMAC-SHA256 with a per-row random
  salt. When ``AUTH_SECRET`` is unset, a deterministic dev key is derived
  so the feature still works locally (with a loud warning logged).
* **Never echoed.** The only values ever returned to the frontend are a
  *masked* representation (e.g. ``"••••••••Ab8R"``) and a boolean
  ``has_key``. The plaintext is only ever read server-side by the AI
  provider factory / GitHub integration.
* **Backward compatible.** When auth is disabled (the default) all secrets
  are stored under the ``"default"`` user key, so the single-user local-dev
  experience is unchanged.
* **Serverless-aware.** Uses the same DB path as the rest of the app
  (redirected to ``/tmp`` on Vercel by ``Settings.db_path``).

Supported secret kinds
----------------------
* ``ai_api_key``  — a user-provided AI provider API key (OpenAI / Gemini /
  Jules / Anthropic / generic). Used by ``get_provider()`` when present,
  ahead of env-var keys and the built-in default.
* ``github_token`` — a user-provided GitHub personal-access token, used to
  give the agent read/write access to a GitHub repository for coding
  (clone, commit, push, PRs).
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
from pathlib import Path
from typing import Any, Dict, Optional

import aiosqlite

log = logging.getLogger("pk_ninja.secrets")

# The canonical set of secret "kinds" we know how to store. Storing an
# unknown kind is rejected so the table cannot be abused as a generic
# key/value dump.
SECRET_KINDS = frozenset({"ai_api_key", "github_token"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_secrets (
    user_id   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    salt      TEXT NOT NULL,
    nonce     TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, kind)
);
"""

# PBKDF2 parameters for deriving the per-row AES key from the master secret.
_PBKDF2_ITERS = 120_000
_PBKDF2_DKLEN = 32  # 256-bit key for AES-256


def _default_user_id(user: Any) -> str:
    """Resolve the storage key for a user (defaults to 'default')."""
    if user is None:
        return "default"
    uid = getattr(user, "user_id", None) or "anonymous"
    return uid if uid != "anonymous" else "default"


def _master_secret(settings: Any) -> bytes:
    """Derive the master encryption secret from AUTH_SECRET.

    If AUTH_SECRET is not configured we derive a deterministic *dev* key
    from a fixed label. This keeps the feature working locally but is
    explicitly NOT secure for production — a warning is logged. Operators
    who want real at-rest protection must set AUTH_SECRET.
    """
    configured = getattr(settings, "auth_secret", "") or ""
    if configured:
        return configured.encode("utf-8")
    log.warning(
        "AUTH_SECRET is not set; user API keys are stored with a deterministic "
        "dev key. Set AUTH_SECRET for encryption at rest in production."
    )
    # Deterministic dev key — same process, same key. Not secure, but
    # functional for local development and ephemeral serverless deploys.
    return b"pk-ninja-dev-secret-do-not-use-in-production"


def _derive_key(master: bytes, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from the master secret + per-row salt."""
    return hashlib.pbkdf2_hmac("sha256", master, salt, _PBKDF2_ITERS, _PBKDF2_DKLEN)


def _encrypt(plaintext: str, settings: Any) -> Dict[str, str]:
    """Encrypt ``plaintext`` and return ``{salt, nonce, ciphertext}`` (b64)."""
    salt = _secrets.token_bytes(16)
    nonce = _secrets.token_bytes(12)  # 96-bit nonce for GCM
    key = _derive_key(_master_secret(settings), salt)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception:  # noqa: BLE001 — optional dependency fallback
        # Fallback: XOR-based obfuscation if cryptography isn't installed.
        # This is NOT real encryption, but preserves functionality. A loud
        # warning is logged so operators know to install cryptography.
        log.warning(
            "cryptography package not installed; using obfuscation fallback "
            "for user API keys. Install 'cryptography' for AES-256-GCM."
        )
        ct = bytearray(plaintext.encode("utf-8"))
        ks = hashlib.sha256(key + nonce + salt).digest()
        for i in range(len(ct)):
            ct[i] ^= ks[i % len(ks)]
        return {
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(bytes(ct)).decode("ascii"),
            "algo": "xor-fallback",
        }
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return {
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ct).decode("ascii"),
        "algo": "aes-256-gcm",
    }


def _decrypt(row: Dict[str, Any], settings: Any) -> str:
    """Decrypt a stored secret row back to plaintext."""
    salt = base64.b64decode(row["salt"])
    nonce = base64.b64decode(row["nonce"])
    ct = base64.b64decode(row["ciphertext"])
    algo = row.get("algo", "aes-256-gcm")
    key = _derive_key(_master_secret(settings), salt)
    if algo == "xor-fallback":
        pt = bytearray(ct)
        ks = hashlib.sha256(key + nonce + salt).digest()
        for i in range(len(pt)):
            pt[i] ^= ks[i % len(ks)]
        return pt.decode("utf-8")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


async def _connect(db_path: Path) -> aiosqlite.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA)
    await conn.commit()
    return conn


def mask_secret(value: str, keep: int = 4) -> str:
    """Return a masked representation of a secret for the frontend.

    Shows the last ``keep`` characters and replaces the rest with bullets,
    e.g. ``"AQ.xxxx...xxxx"`` ->
    ``"••••••••••••••••••••••••••••••••••••••••••••••••••••Q4w"``.
    Never reveals the prefix (which is often the most identifying part).
    """
    if not value:
        return ""
    if len(value) <= keep:
        return "•" * len(value)
    return "•" * (len(value) - keep) + value[-keep:]


async def store_secret(settings: Any, user: Any, kind: str, value: str) -> bool:
    """Persist (encrypt) a secret of ``kind`` for ``user``.

    Returns True on success. Raises ``ValueError`` for an unknown kind or
    empty value.
    """
    if kind not in SECRET_KINDS:
        raise ValueError(f"unknown secret kind: {kind}")
    value = (value or "").strip()
    if not value:
        raise ValueError("empty secret value")
    db_path = Path(getattr(settings, "db_path", "pk_ninja.db"))
    uid = _default_user_id(user)
    enc = _encrypt(value, settings)
    now = _dt.datetime.utcnow().isoformat()
    conn = await _connect(db_path)
    try:
        await conn.execute(
            "INSERT INTO user_secrets (user_id, kind, salt, nonce, ciphertext, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, kind) DO UPDATE SET "
            "salt=excluded.salt, nonce=excluded.nonce, ciphertext=excluded.ciphertext, "
            "updated_at=excluded.updated_at",
            (uid, kind, enc["salt"], enc["nonce"], json.dumps(enc), now),
        )
        await conn.commit()
    finally:
        await conn.close()
    return True


async def get_secret(settings: Any, user: Any, kind: str) -> Optional[str]:
    """Return the decrypted plaintext secret, or None if not stored."""
    if kind not in SECRET_KINDS:
        raise ValueError(f"unknown secret kind: {kind}")
    db_path = Path(getattr(settings, "db_path", "pk_ninja.db"))
    uid = _default_user_id(user)
    try:
        conn = await _connect(db_path)
        try:
            cursor = await conn.execute(
                "SELECT ciphertext FROM user_secrets WHERE user_id = ? AND kind = ?",
                (uid, kind),
            )
            row = await cursor.fetchone()
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("secret read failed for %s/%s: %s", uid, kind, exc)
        return None
    if not row:
        return None
    try:
        enc = json.loads(row["ciphertext"])
        return _decrypt(enc, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("secret decrypt failed for %s/%s: %s", uid, kind, exc)
        return None


async def delete_secret(settings: Any, user: Any, kind: str) -> bool:
    """Remove a stored secret. Returns True if a row was deleted."""
    if kind not in SECRET_KINDS:
        raise ValueError(f"unknown secret kind: {kind}")
    db_path = Path(getattr(settings, "db_path", "pk_ninja.db"))
    uid = _default_user_id(user)
    conn = await _connect(db_path)
    try:
        cursor = await conn.execute(
            "DELETE FROM user_secrets WHERE user_id = ? AND kind = ?",
            (uid, kind),
        )
        await conn.commit()
        return cursor.rowcount > 0
    finally:
        await conn.close()


async def list_secrets(settings: Any, user: Any) -> Dict[str, str]:
    """Return a dict of ``{kind: masked_value}`` for all stored secrets.

    Only the *masked* representation is returned — never the plaintext.
    """
    db_path = Path(getattr(settings, "db_path", "pk_ninja.db"))
    uid = _default_user_id(user)
    out: Dict[str, str] = {}
    try:
        conn = await _connect(db_path)
        try:
            cursor = await conn.execute(
                "SELECT kind, ciphertext FROM user_secrets WHERE user_id = ?",
                (uid,),
            )
            rows = await cursor.fetchall()
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("secret list failed for %s: %s", uid, exc)
        return out
    for row in rows:
        kind = row["kind"]
        if kind not in SECRET_KINDS:
            continue
        try:
            enc = json.loads(row["ciphertext"])
            plain = _decrypt(enc, settings)
            out[kind] = mask_secret(plain)
        except Exception as exc:  # noqa: BLE001
            log.warning("secret mask failed for %s/%s: %s", uid, kind, exc)
            out[kind] = "••••••••"
    return out


async def has_secret(settings: Any, user: Any, kind: str) -> bool:
    """Return True if a secret of ``kind`` is stored for ``user``."""
    return (await get_secret(settings, user, kind)) is not None
