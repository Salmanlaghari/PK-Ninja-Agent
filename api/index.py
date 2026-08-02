"""Vercel serverless entry point for the PK-Ninja-Agent FastAPI app.

Vercel's Python runtime (``@vercel/python``) looks for an ASGI application
named ``app`` in each file under ``api/``. This file re-exports the FastAPI
``app`` from ``backend/main.py`` so the entire API + static frontend is served
from a single serverless function.

The ``backend`` package directory is added to ``sys.path`` at import time so
that the intra-package imports in ``backend/*.py`` (e.g. ``from config import
get_settings``) resolve correctly under Vercel's flat runtime.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the backend/ package importable. On Vercel the function file is copied
# to a build output dir, so we resolve relative to this file's location and
# also fall back to the repo root layout (``<repo>/backend``).
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
# Also add the repo root so ``backend`` can be imported as a package if needed.
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Ensure a writable CWD-side DB path on serverless (config.is_serverless also
# checks VERCEL=1, but we belt-and-brace the TMPDIR here).
os.environ.setdefault("VERCEL", "1")

from main import app  # noqa: E402  — import after sys.path setup
