"""Vercel serverless entry point for the PK-Ninja-Agent FastAPI app.

Vercel's Python runtime (``@vercel/python``) looks for an ASGI application
named ``app`` in each file under ``api/``. This file re-exports the FastAPI
``app`` from ``backend/main.py`` so the entire API + static frontend is served
from a single serverless function.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Path setup for Vercel serverless ──────────────────────────────────
# On Vercel the function runs from /var/task with this layout:
#   /var/task/api/index.py
#   /var/task/backend/   (copied via includeFiles)
#   /var/task/frontend/  (copied via includeFiles)
#   /var/task/providers/ (copied via includeFiles)
_HERE = Path(__file__).resolve().parent       # /var/task/api
_PROJECT_ROOT = _HERE.parent                  # /var/task

# Add project root so `from backend.xxx import ...` works
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Add backend/ so bare imports (from agent import ...) work
_BACKEND = _PROJECT_ROOT / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Mark as serverless
os.environ.setdefault("VERCEL", "1")

from main import app  # noqa: E402
