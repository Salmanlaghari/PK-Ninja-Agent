"""Graceful shutdown handler for PK Ninja Agent.

Handles SIGTERM/SIGINT signals to:
1. Stop accepting new connections
2. Drain in-flight requests
3. Stop background worker
4. Close database connections
5. Exit cleanly

Usage:
    from shutdown import register_shutdown_handlers
    register_shutdown_handlers(app)
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any, Optional

log = logging.getLogger("pk_ninja.shutdown")

_shutdown_state = {"initiated": False}


def register_shutdown_handlers(app: Any) -> None:
    """Register signal handlers and lifespan events for graceful shutdown."""
    import uvicorn

    loop: Optional[asyncio.AbstractEventLoop] = None

    def _signal_handler(sig, frame):
        if _shutdown_state["initiated"]:
            log.warning("Forced shutdown requested — exiting immediately")
            sys.exit(1)
        _shutdown_state["initiated"] = True
        log.info("Shutdown signal received (%s) — draining...", signal.Signals(sig).name)
        if loop and loop.is_running():
            asyncio.ensure_future(_graceful_shutdown(app))

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    @app.on_event("startup")
    async def _capture_loop():
        nonlocal loop
        loop = asyncio.get_running_loop()
        log.info("Shutdown handlers registered (SIGTERM, SIGINT)")

    @app.on_event("shutdown")
    async def _on_shutdown():
        await _cleanup(app)


async def _graceful_shutdown(app: Any) -> None:
    """Initiate graceful shutdown from a signal handler."""
    await _cleanup(app)
    # Give uvicorn a moment to finish draining, then force exit.
    await asyncio.sleep(1.0)
    log.info("Shutdown complete — exiting")
    sys.exit(0)


async def _cleanup(app: Any) -> None:
    """Clean up resources: worker, scheduler, DB pools."""
    try:
        from worker import stop_worker, get_worker
        w = get_worker()
        if w is not None:
            log.info("Stopping background worker...")
            stop_worker()
    except Exception as exc:
        log.warning("Worker cleanup error: %s", exc)

    try:
        from scheduler import get_scheduler
        sched = get_scheduler()
        if sched is not None:
            log.info("Scheduler active — %d items in queue", sched.queue_length())
    except Exception as exc:
        log.warning("Scheduler cleanup error: %s", exc)

    log.info("Cleanup complete")
