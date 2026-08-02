"""Structured JSON logging for PK Ninja Agent.

Replaces plain-text logs with JSON-structured output for production
environments, making logs machine-parseable for ELK, Loki, Datadog, etc.

Usage:
    from structured_logging import setup_logging
    setup_logging(json_format=True, log_level="INFO")
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Include extra fields from record
        for key in ("task_id", "user_id", "request_id", "method", "path",
                     "status_code", "duration_ms", "component"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val

        # Include exception info
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Unknown",
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(*record.exc_info),
            }

        return json.dumps(log_entry, default=str, ensure_ascii=False)


class RequestContextFilter(logging.Filter):
    """Inject request context (request_id, method, path) into log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        # These will be populated by middleware
        for attr in ("request_id", "method", "path", "status_code", "duration_ms"):
            if not hasattr(record, attr):
                setattr(record, attr, None)
        return True


def setup_logging(
    json_format: bool = False,
    log_level: str = "INFO",
    log_file: Optional[str] = None,
) -> None:
    """Configure application logging.

    Args:
        json_format: If True, emit JSON-structured logs (production mode).
        log_level: Minimum log level (DEBUG, INFO, WARNING, ERROR).
        log_file: Optional file path for log output.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Remove existing handlers
    root.handlers.clear()

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    if json_format:
        console.setFormatter(JSONFormatter())
    else:
        console.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    console.addFilter(RequestContextFilter())
    root.addHandler(console)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JSONFormatter())
        file_handler.addFilter(RequestContextFilter())
        root.addHandler(file_handler)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


class RequestLoggingMiddleware:
    """ASGI middleware that logs every request with timing and status."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        import time
        import uuid

        start = time.monotonic()
        request_id = str(uuid.uuid4())[:8]
        method = scope.get("method", "?")
        path = scope.get("path", "?")

        # Store request_id on scope for downstream access
        scope["request_id"] = request_id

        log = logging.getLogger("pk_ninja.access")

        status_code = 500
        response_started = False

        async def send_wrapper(message):
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status_code = 500
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            extra = {
                "request_id": request_id,
                "method": method,
                "path": path,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 1),
            }
            msg = f"{method} {path} → {status_code} ({duration_ms:.0f}ms)"
            if status_code >= 500:
                log.error(msg, extra=extra)
            elif status_code >= 400:
                log.warning(msg, extra=extra)
            else:
                log.info(msg, extra=extra)
