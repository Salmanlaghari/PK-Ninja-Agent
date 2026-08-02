"""Prometheus metrics endpoint for PK Ninja Agent.

Exposes /metrics for scraping by Prometheus or compatible systems.

Usage:
    from metrics import setup_metrics
    setup_metrics(app)

Requires: prometheus_client (optional, graceful degradation)
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("pk_ninja.metrics")

try:
    from prometheus_client import (
        Counter, Gauge, Histogram, Info,
        generate_latest, CONTENT_TYPE_LATEST,
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    log.info("prometheus_client not installed — /metrics endpoint disabled")

# ── Metric definitions (only when prometheus_client is available) ────────
if PROMETHEUS_AVAILABLE:
    TASKS_TOTAL = Counter(
        "pk_ninja_tasks_total",
        "Total number of tasks created",
        ["status"],
    )
    TASK_DURATION = Histogram(
        "pk_ninja_task_duration_seconds",
        "Task execution duration in seconds",
        buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600],
    )
    ACTIVE_TASKS = Gauge(
        "pk_ninja_active_tasks",
        "Number of currently running tasks",
    )
    QUEUE_SIZE = Gauge(
        "pk_ninja_queue_size",
        "Number of tasks in the scheduler queue",
    )
    WORKER_ACTIVE = Gauge(
        "pk_ninja_worker_active",
        "Number of tasks currently being executed by workers",
    )
    HTTP_REQUESTS = Counter(
        "pk_ninja_http_requests_total",
        "Total HTTP requests",
        ["method", "path", "status"],
    )
    HTTP_DURATION = Histogram(
        "pk_ninja_http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["method", "path"],
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )
    DB_OPERATIONS = Counter(
        "pk_ninja_db_operations_total",
        "Total database operations",
        ["operation", "table"],
    )
    PROVIDER_CALLS = Counter(
        "pk_ninja_provider_calls_total",
        "Total AI provider calls",
        ["provider", "status"],
    )
    PROVIDER_LATENCY = Histogram(
        "pk_ninja_provider_latency_seconds",
        "AI provider call latency",
        ["provider"],
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
    )
    APP_INFO = Info(
        "pk_ninja",
        "PK Ninja Agent application info",
    )


def setup_metrics(app: Any) -> None:
    """Register the /metrics endpoint and request timing middleware."""
    if not PROMETHEUS_AVAILABLE:
        log.info("Metrics disabled — install prometheus_client to enable")
        return

    from fastapi.responses import PlainTextResponse

    APP_INFO.info({
        "version": "1.0.0",
        "name": "pk-ninja-agent",
    })

    @app.get("/metrics")
    async def metrics_endpoint():
        """Prometheus scrape endpoint."""
        data = generate_latest()
        return PlainTextResponse(
            content=data,
            media_type=CONTENT_TYPE_LATEST,
        )

    # Request timing middleware
    @app.middleware("http")
    async def metrics_middleware(request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - start

        method = request.method
        path = request.url.path
        status = str(response.status_code)

        # Skip metrics endpoint itself to avoid recursion
        if path != "/metrics":
            HTTP_REQUESTS.labels(method=method, path=path, status=status).inc()
            HTTP_DURATION.labels(method=method, path=path).observe(duration)

        return response

    log.info("Prometheus metrics enabled at /metrics")


def record_task_created(status: str = "created") -> None:
    """Record a task creation event."""
    if PROMETHEUS_AVAILABLE:
        TASKS_TOTAL.labels(status=status).inc()


def record_task_duration(seconds: float) -> None:
    """Record task execution duration."""
    if PROMETHEUS_AVAILABLE:
        TASK_DURATION.observe(seconds)


def set_active_tasks(count: int) -> None:
    """Set the current active tasks gauge."""
    if PROMETHEUS_AVAILABLE:
        ACTIVE_TASKS.set(count)


def set_queue_size(count: int) -> None:
    """Set the current queue size gauge."""
    if PROMETHEUS_AVAILABLE:
        QUEUE_SIZE.set(count)


def set_worker_active(count: int) -> None:
    """Set the current worker active count gauge."""
    if PROMETHEUS_AVAILABLE:
        WORKER_ACTIVE.set(count)


def record_provider_call(provider: str, status: str, latency: float) -> None:
    """Record an AI provider call."""
    if PROMETHEUS_AVAILABLE:
        PROVIDER_CALLS.labels(provider=provider, status=status).inc()
        PROVIDER_LATENCY.labels(provider=provider).observe(latency)


def record_db_operation(operation: str, table: str) -> None:
    """Record a database operation."""
    if PROMETHEUS_AVAILABLE:
        DB_OPERATIONS.labels(operation=operation, table=table).inc()
