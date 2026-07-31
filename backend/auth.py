"""Modular authentication for PK Ninja Agent (v0.7.0).

Design goals
------------
* **Opt-in & backward compatible.** ``AUTH_ENABLED=false`` (the default) means
  no authentication is required at all — every request is treated as a guest.
  This preserves the original behaviour so existing tests and local dev keep
  working unchanged.
* **GitHub login (optional).** When ``AUTH_GITHUB_ENABLED=true`` and a
  ``GITHUB_TOKEN`` is configured server-side, a user can "Sign in with GitHub"
  by providing their own token; the server validates it against the GitHub API
  and creates a session. No OAuth app is required for the beta — we trade the
  OAuth dance for a simple token check, which is sufficient for a single-user
  / small-team beta.
* **Guest mode.** When ``AUTH_GUEST_ALLOWED=true`` (default), a user can
  continue as a guest with a short-lived ephemeral session and no GitHub
  identity.
* **Sessions.** Sessions are signed tokens (HMAC-SHA256) stored entirely
  client-side as an ``Authorization: Bearer <token>`` header (or
  ``?session=`` query param). The token payload is JSON with the user info +
  expiry; the signature prevents tampering. No server-side session store is
  needed for the beta, keeping the system stateless and simple.
* **Modularity.** ``AuthService`` is the single entry point. The FastAPI
  dependency ``require_user`` returns the current ``User`` (a real user, a
  guest, or an ``AUTH_DISABLED`` placeholder when auth is off). All
  provider/agent/workspace code stays unaware of auth.

No secrets are ever logged or serialized into responses beyond the session
token itself (which is required to make authenticated requests).
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

try:  # httpx is only needed for GitHub token verification (optional).
    import httpx  # noqa: F401  (re-exported so tests can monkeypatch it)
except Exception:  # noqa: BLE001
    httpx = None  # type: ignore[assignment]

log = logging.getLogger("pk_ninja.auth")

# Default session lifetime for guests (short) and authenticated users (longer).
_GUEST_TTL_SECONDS = 60 * 60 * 4          # 4 hours
_USER_TTL_SECONDS = 60 * 60 * 24 * 7      # 7 days


@dataclass
class User:
    """A resolved user identity (or the anonymous placeholder)."""
    user_id: str
    username: str
    display_name: str
    is_guest: bool = True
    github_login: Optional[str] = None
    avatar_url: Optional[str] = None
    scopes: list = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "is_guest": self.is_guest,
            "github_login": self.github_login,
            "avatar_url": self.avatar_url,
            "scopes": list(self.scopes),
        }


# The placeholder user returned when auth is disabled (backward compat).
_DISABLED_USER = User(
    user_id="anonymous",
    username="anonymous",
    display_name="Anonymous",
    is_guest=True,
)


class AuthError(Exception):
    """Base authentication / authorization error."""


class InvalidTokenError(AuthError):
    """The session token is missing, malformed, tampered, or expired."""


class AuthService:
    """Stateless session-token auth with optional GitHub login + guest mode."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.enabled: bool = bool(getattr(settings, "auth_enabled", False))
        self.guest_allowed: bool = bool(getattr(settings, "auth_guest_allowed", True))
        self.github_enabled: bool = bool(getattr(settings, "auth_github_enabled", False))
        # Signing secret. If not provided, generate a random one per process
        # (sessions won't survive a restart, which is acceptable for tests/dev).
        configured = getattr(settings, "auth_secret", "") or ""
        self._secret: bytes = (configured.encode("utf-8") if configured
                               else secrets.token_bytes(32))
        self._guest_ttl = getattr(settings, "auth_guest_ttl_seconds", _GUEST_TTL_SECONDS) or _GUEST_TTL_SECONDS
        self._user_ttl = getattr(settings, "auth_user_ttl_seconds", _USER_TTL_SECONDS) or _USER_TTL_SECONDS

    # ── token (de)serialisation ────────────────────────────────────────────
    def _sign(self, payload_b64: str) -> str:
        return hmac.new(self._secret, payload_b64.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def create_session(self, user: User, ttl_seconds: Optional[int] = None) -> str:
        """Create a signed session token for ``user``."""
        ttl = ttl_seconds if ttl_seconds is not None else (
            self._guest_ttl if user.is_guest else self._user_ttl)
        now = _dt.datetime.utcnow()
        payload = {
            "sub": user.user_id,
            "username": user.username,
            "display_name": user.display_name,
            "is_guest": user.is_guest,
            "github_login": user.github_login,
            "avatar_url": user.avatar_url,
            "scopes": list(user.scopes),
            "iat": int(now.timestamp()),
            "exp": int((now + _dt.timedelta(seconds=ttl)).timestamp()),
        }
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii")
        sig = self._sign(payload_b64)
        return f"{payload_b64}.{sig}"

    def verify_session(self, token: str) -> User:
        """Verify a session token and return the resolved :class:`User`.

        Raises :class:`InvalidTokenError` if the token is missing, tampered,
        or expired.
        """
        if not token or "." not in token:
            raise InvalidTokenError("malformed token")
        payload_b64, _, sig = token.rpartition(".")
        if not payload_b64 or not sig:
            raise InvalidTokenError("malformed token")
        expected_sig = self._sign(payload_b64)
        if not hmac.compare_digest(sig, expected_sig):
            raise InvalidTokenError("invalid signature")
        try:
            payload_json = base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8")
            payload = json.loads(payload_json)
        except Exception as exc:  # noqa: BLE001
            raise InvalidTokenError("undecodable payload") from exc
        exp = payload.get("exp")
        if exp is None or _dt.datetime.utcnow().timestamp() > exp:
            raise InvalidTokenError("token expired")
        return User(
            user_id=payload.get("sub", ""),
            username=payload.get("username", ""),
            display_name=payload.get("display_name", ""),
            is_guest=bool(payload.get("is_guest", True)),
            github_login=payload.get("github_login"),
            avatar_url=payload.get("avatar_url"),
            scopes=list(payload.get("scopes", [])),
        )

    # ── login flows ─────────────────────────────────────────────────────────
    def login_guest(self, display_name: str = "Guest") -> User:
        """Create an ephemeral guest user (if guest mode is allowed)."""
        if not self.guest_allowed:
            raise AuthError("Guest mode is disabled.")
        gid = "guest_" + secrets.token_hex(6)
        user = User(
            user_id=gid,
            username=gid,
            display_name=display_name,
            is_guest=True,
        )
        log.info("guest login: %s", gid)
        return user

    def login_github(self, github_token: str) -> User:
        """Validate a GitHub token and create an authenticated user.

        Calls the GitHub ``/user`` endpoint to verify the token and read the
        user's identity. Does **not** store the token anywhere persistent; the
        token is only used for this verification call and then discarded.
        """
        if not self.github_enabled:
            raise AuthError("GitHub login is disabled.")
        if not github_token or not github_token.strip():
            raise AuthError("A GitHub token is required.")
        info = self._verify_github_token(github_token.strip())
        if not info:
            raise AuthError("GitHub token verification failed.")
        user = User(
            user_id="gh_" + str(info.get("id", "")),
            username=info.get("login", "github-user"),
            display_name=info.get("name") or info.get("login", "GitHub User"),
            is_guest=False,
            github_login=info.get("login"),
            avatar_url=info.get("avatar_url"),
            scopes=self._github_scopes(github_token.strip(), info),
        )
        log.info("github login: %s", user.username)
        return user

    def logout(self, token: str) -> None:
        """Stateless logout — the client simply discards the token.

        We record nothing server-side; the token is invalid once the client
        stops sending it (and it will expire on its own). This method exists
        for API symmetry and logging.
        """
        log.info("logout requested (token len=%d)", len(token or ""))

    # ── helpers ─────────────────────────────────────────────────────────────
    def _verify_github_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Call GitHub's /user endpoint to verify the token. None on failure."""
        if httpx is None:
            log.warning("httpx not installed; cannot verify GitHub token")
            return None
        try:
            # Trim common prefixes so users can paste raw tokens.
            t = token
            headers = {
                "Authorization": f"Bearer {t}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            with httpx.Client(timeout=15.0) as client:
                r = client.get("https://api.github.com/user", headers=headers)
                if r.status_code == 200:
                    return r.json()
                log.warning("github token verify failed: %s", r.status_code)
                return None
        except Exception as exc:  # noqa: BLE001
            log.warning("github token verify error: %s", exc)
            return None

    def _github_scopes(self, token: str, info: Dict[str, Any]) -> list:
        """Best-effort scope detection from the /user response (no extra call)."""
        scopes = []
        if info.get("login"):
            scopes.append("repo:read")
        # We don't probe the X-OAuth-Scopes header to avoid an extra request;
        # callers that need write scopes can request them explicitly.
        return scopes

    def require_user_from_request(self, authorization: Optional[str],
                                  query_session: Optional[str]) -> User:
        """Resolve the current user from an HTTP request.

        * If auth is **disabled**, return the anonymous placeholder (backward
          compat — every endpoint is open).
        * If auth is **enabled**, require a valid session token (from the
          ``Authorization: Bearer`` header or the ``?session=`` query param).
          Missing/invalid → raise :class:`InvalidTokenError`.
        """
        if not self.enabled:
            return _DISABLED_USER
        token = None
        if authorization:
            # Accept either a raw token or an "Authorization: Bearer <token>"
            # header value (the FastAPI HTTPBearer dependency already strips the
            # "Bearer " prefix, but callers may pass the raw header too).
            authz = authorization.strip()
            if authz.lower().startswith("bearer "):
                token = authz.split(" ", 1)[1].strip()
            else:
                token = authz
        elif query_session:
            token = query_session.strip()
        if not token:
            raise InvalidTokenError("Authentication required.")
        return self.verify_session(token)


# ── module-level singleton ─────────────────────────────────────────────────
_service: Optional[AuthService] = None


def get_auth_service(settings: Optional[Any] = None) -> AuthService:
    """Return the shared :class:`AuthService` (built from current settings)."""
    global _service
    if settings is not None:
        _service = AuthService(settings)
        return _service
    if _service is None:
        from config import get_settings
        _service = AuthService(get_settings())
    return _service


def reset_auth_service() -> None:
    """Reset the cached service (used by tests)."""
    global _service
    _service = None
