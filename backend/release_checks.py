"""Release-preparation startup checks & environment validation (v0.7.0).

Run lightweight, non-blocking checks at startup so the operator gets a clear
picture of the deployment health. No check ever raises — they return a list of
``{name, status, detail}`` dicts that the startup hook logs and the
``/api/system/health`` endpoint surfaces to the UI.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("pk_ninja.release_checks")

_OK = "ok"
_WARN = "warn"
_DOWN = "down"


def _check_workspace_root(settings: Any) -> Dict[str, Any]:
    try:
        p = settings.workspace_root_path
        return {"name": "workspace_root", "status": _OK,
                "detail": f"writable at {p}"}
    except Exception as exc:  # noqa: BLE001
        return {"name": "workspace_root", "status": _DOWN, "detail": str(exc)}


def _check_database(settings: Any) -> Dict[str, Any]:
    try:
        p = settings.db_path
        p.parent.mkdir(parents=True, exist_ok=True)
        # Touch the DB file to confirm writability.
        Path(p).touch(exist_ok=True)
        return {"name": "database", "status": _OK, "detail": f"writable at {p}"}
    except Exception as exc:  # noqa: BLE001
        return {"name": "database", "status": _DOWN, "detail": str(exc)}


def _check_github(settings: Any) -> Dict[str, Any]:
    if not settings.github_repo_full():
        return {"name": "github", "status": _WARN,
                "detail": "GITHUB_OWNER/GITHUB_REPO not set; git push/PR disabled"}
    if not getattr(settings, "github_token", ""):
        return {"name": "github", "status": _WARN,
                "detail": "GITHUB_TOKEN not set; authenticated git ops will fail"}
    return {"name": "github", "status": _OK,
            "detail": f"configured for {settings.github_repo_full()}"}


def _check_ai_provider(settings: Any) -> Dict[str, Any]:
    provider = settings.ai_provider or "local"
    if provider == "local":
        return {"name": "ai_provider", "status": _OK,
                "detail": "local (offline, no key required)"}
    key = settings.effective_api_key() if hasattr(settings, "effective_api_key") else ""
    if not key:
        return {"name": "ai_provider", "status": _WARN,
                "detail": f"{provider} selected but no API key set; will fall back to local"}
    return {"name": "ai_provider", "status": _OK,
            "detail": f"{provider} configured"}


def _check_python_version() -> Dict[str, Any]:
    vi = sys.version_info
    if vi >= (3, 10):
        return {"name": "python", "status": _OK,
                "detail": f"{vi.major}.{vi.minor}.{vi.micro}"}
    return {"name": "python", "status": _WARN,
            "detail": f"{vi.major}.{vi.minor}.{vi.micro} (3.10+ recommended)"}


def _check_production_safety(settings: Any) -> Dict[str, Any]:
    env = getattr(settings, "app_env", "development")
    debug = getattr(settings, "debug", False)
    if env == "production":
        if debug:
            return {"name": "production_safety", "status": _WARN,
                    "detail": "DEBUG=true in production — disable for release"}
        if not getattr(settings, "auth_enabled", False):
            return {"name": "production_safety", "status": _WARN,
                    "detail": "AUTH_ENABLED=false in production — enable auth"}
        return {"name": "production_safety", "status": _OK,
                "detail": "production hardening OK"}
    return {"name": "production_safety", "status": _OK,
            "detail": f"env={env} (no production hardening required)"}


def run_startup_checks(settings: Any) -> List[Dict[str, Any]]:
    """Run all startup checks and return their results (never raises)."""
    checks = [
        _check_python_version(),
        _check_workspace_root(settings),
        _check_database(settings),
        _check_github(settings),
        _check_ai_provider(settings),
        _check_production_safety(settings),
    ]
    return checks


def system_health(settings: Any) -> Dict[str, Any]:
    """Return an aggregated system-health snapshot for the dashboard/UI."""
    checks = run_startup_checks(settings)
    statuses = [c["status"] for c in checks]
    if any(s == _DOWN for s in statuses):
        overall = "down"
    elif any(s == _WARN for s in statuses):
        overall = "degraded"
    else:
        overall = "ok"
    return {
        "status": overall,
        "version": "0.7.0",
        "environment": getattr(settings, "app_env", "development"),
        "components": checks,
    }
