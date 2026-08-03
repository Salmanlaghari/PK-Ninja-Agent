"""v1.2.0 — Rate Limiting & CSRF Protection.

Provides per-user rate limiting on auth endpoints and CSRF tokens
for state-changing requests.

Design:
- In-memory token bucket per IP/user (no external deps like Redis).
- Configurable via env vars: RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW.
- CSRF tokens: generated per-session, validated on POST/PUT/DELETE.
- Rate limit headers included in responses (X-RateLimit-*).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("pk_ninja.rate_limiter")


# ── Configuration ──────────────────────────────────────────────────────────

_RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "1000"))
_RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))  # seconds
_AUTH_RATE_LIMIT_REQUESTS = int(os.environ.get("AUTH_RATE_LIMIT_REQUESTS", "100"))
_AUTH_RATE_LIMIT_WINDOW = int(os.environ.get("AUTH_RATE_LIMIT_WINDOW", "60"))
_CSRF_SECRET = os.environ.get("CSRF_SECRET", "")  # empty = auto-generate per process
_csrf_secret_key = _CSRF_SECRET or secrets.token_hex(32)


# ── Token Bucket Rate Limiter ─────────────────────────────────────────────

class RateLimiter:
    """Simple in-memory token bucket rate limiter.

    Each key (IP address or user ID) gets a bucket that refills at a
    constant rate. Requests consume tokens; when empty, requests are
    rejected with 429.
    """

    def __init__(
        self,
        max_requests: int = 60,
        window_seconds: int = 60,
    ) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._buckets: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, last_refill)
        self._lock = __import__("threading").Lock()

    def _refill(self, key: str) -> Tuple[float, float]:
        """Refill tokens for a key. Returns (tokens, last_refill)."""
        now = time.monotonic()
        entry = self._buckets.get(key)
        if entry is None:
            return (float(self._max - 1), now)
        tokens, last = entry
        elapsed = now - last
        refill = elapsed * (self._max / self._window)
        tokens = min(float(self._max), tokens + refill)
        return (tokens - 1, now)

    def check(self, key: str) -> Tuple[bool, Dict[str, str]]:
        """Check if request is allowed. Returns (allowed, headers)."""
        with self._lock:
            tokens, last = self._refill(key)
            self._buckets[key] = (tokens, last)
            remaining = max(0, int(tokens))
            retry_after = max(0, int(self._window * (1 - tokens / self._max))) if tokens < 0 else 0
            headers = {
                "X-RateLimit-Limit": str(self._max),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(int(last + self._window)),
            }
            if tokens < 0:
                headers["Retry-After"] = str(retry_after)
                return False, headers
            return True, headers

    def cleanup(self, max_age: float = 300.0) -> int:
        """Remove stale entries older than max_age seconds."""
        now = time.monotonic()
        with self._lock:
            stale = [k for k, (_, last) in self._buckets.items() if now - last > max_age]
            for k in stale:
                del self._buckets[k]
            return len(stale)


# ── Global instances ───────────────────────────────────────────────────────

_general_limiter = RateLimiter(_RATE_LIMIT_REQUESTS, _RATE_LIMIT_WINDOW)
_auth_limiter = RateLimiter(_AUTH_RATE_LIMIT_REQUESTS, _AUTH_RATE_LIMIT_WINDOW)


def get_general_limiter() -> RateLimiter:
    return _general_limiter


def get_auth_limiter() -> RateLimiter:
    return _auth_limiter


# ── CSRF Token Management ─────────────────────────────────────────────────

def generate_csrf_token(session_id: str = "") -> str:
    """Generate a CSRF token bound to the session."""
    nonce = secrets.token_hex(16)
    sig = hmac.new(
        _csrf_secret_key.encode(),
        f"{session_id}:{nonce}".encode(),
        hashlib.sha256,
    ).hexdigest()[:16]
    return f"{nonce}.{sig}"


def validate_csrf_token(token: str, session_id: str = "") -> bool:
    """Validate a CSRF token."""
    if not token or "." not in token:
        return False
    try:
        nonce, sig = token.split(".", 1)
        expected = hmac.new(
            _csrf_secret_key.encode(),
            f"{session_id}:{nonce}".encode(),
            hashlib.sha256,
        ).hexdigest()[:16]
        return hmac.compare_digest(sig, expected)
    except (ValueError, AttributeError):
        return False


def csrf_token_for_session(session_id: str = "") -> str:
    """Public API: generate a CSRF token."""
    return generate_csrf_token(session_id)


def csrf_protect(session_id: str = "") -> Callable:
    """Decorator: validate CSRF token on POST/PUT/DELETE/PATCH requests."""
    def deco(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapper(request, *args: Any, **kwargs: Any) -> Any:
            method = getattr(request, "method", "GET")
            if method in ("POST", "PUT", "DELETE", "PATCH"):
                # Skip CSRF for API key auth (Bearer token)
                auth = getattr(request, "headers", {}).get("authorization", "")
                if auth.startswith("Bearer "):
                    return await fn(request, *args, **kwargs)
                token = (
                    getattr(request, "headers", {}).get("x-csrf-token", "")
                    or ""
                )
                if not validate_csrf_token(token, session_id):
                    from fastapi.responses import JSONResponse
                    return JSONResponse(
                        {"error": "CSRF token missing or invalid"},
                        status_code=403,
                    )
            return await fn(request, *args, **kwargs)
        return wrapper
    return deco


# ── FastAPI Middleware ─────────────────────────────────────────────────────

def create_rate_limit_middleware():
    """Create a Starlette middleware for rate limiting."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class RateLimitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Determine client key
            client_ip = request.client.host if request.client else "unknown"
            path = request.url.path

            # Use stricter limits for auth endpoints
            is_auth = path.startswith("/api/auth") or path.startswith("/api/login")
            limiter = _auth_limiter if is_auth else _general_limiter
            key = f"{client_ip}:{'auth' if is_auth else 'general'}"

            allowed, headers = limiter.check(key)
            if not allowed:
                response = JSONResponse(
                    {"error": "Rate limit exceeded", "retry_after": headers.get("Retry-After", "60")},
                    status_code=429,
                )
                for k, v in headers.items():
                    response.headers[k] = v
                return response

            response = await call_next(request)
            for k, v in headers.items():
                response.headers[k] = v
            return response

    return RateLimitMiddleware


__all__ = [
    "RateLimiter",
    "get_general_limiter",
    "get_auth_limiter",
    "generate_csrf_token",
    "validate_csrf_token",
    "csrf_token_for_session",
    "csrf_protect",
    "create_rate_limit_middleware",
]
