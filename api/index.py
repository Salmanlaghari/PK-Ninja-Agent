"""Vercel serverless entry point for the PK-Ninja-Agent FastAPI app.

Vercel's Python runtime sets PATH_INFO to the function path (/api/index),
NOT the original request path. We add ASGI middleware to restore the
original path from Vercel's routing headers so FastAPI routes correctly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# ── Path setup ────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_BACKEND = _PROJECT_ROOT / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("VERCEL", "1")

from main import app  # noqa: E402


# ── Path-restoring middleware ─────────────────────────────────────────
class VercelPathMiddleware:
    """ASGI middleware that restores the original request path on Vercel.

    Vercel's Python runtime sets the ASGI scope path to the function's
    path (``/api/index``) instead of the original request path. This
    middleware reads the ``x-vercel-original-url`` or reconstructs the
    path from ``x-forwarded-host`` + ``x-vercel-deployment-url`` to fix
    routing.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> Any:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            # Try to get original path from Vercel headers
            original_path = None

            # Check x-forwarded-uri first (some Vercel versions)
            if b"x-forwarded-uri" in headers:
                original_path = headers[b"x-forwarded-uri"].decode("utf-8").split("?")[0]

            # Check if the path is the function path and fix it
            if scope.get("path", "").startswith("/api/index") and not original_path:
                # The path is the function path, not the original request path
                # Reconstruct from the raw_path or query string
                raw_path = scope.get("raw_path", b"").decode("utf-8")
                if raw_path and raw_path != "/api/index":
                    original_path = raw_path.split("?")[0]

            if original_path and original_path != scope.get("path"):
                scope["path"] = original_path
                # Also fix root_path for proper URL generation
                scope["root_path"] = ""

        return await self.app(scope, receive, send)


# Wrap the FastAPI app with the path-fixing middleware
app = VercelPathMiddleware(app)
