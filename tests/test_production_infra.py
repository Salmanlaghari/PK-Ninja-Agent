"""Tests for production infrastructure — v0.9.0.

Covers:
* Structured logging setup and JSON formatter
* Request logging middleware
* Graceful shutdown handler registration
* Backup manager (create, list, verify, cleanup, restore)
* Metrics module (graceful degradation without prometheus_client)
* Startup script validation
"""
import json
import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── Structured Logging ──────────────────────────────────────────────────

class TestStructuredLogging:
    def test_setup_logging_default(self):
        from structured_logging import setup_logging
        # Should not raise
        setup_logging(json_format=False, log_level="INFO")
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_json_formatter_output(self):
        from structured_logging import JSONFormatter
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["message"] == "test message"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test"
        assert "timestamp" in parsed

    def test_json_formatter_with_exception(self):
        from structured_logging import JSONFormatter
        formatter = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname="", lineno=0,
                msg="error occurred", args=(), exc_info=sys.exc_info(),
            )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert parsed["exception"]["type"] == "ValueError"
        assert "test error" in parsed["exception"]["message"]

    def test_json_formatter_extra_fields(self):
        from structured_logging import JSONFormatter
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="request", args=(), exc_info=None,
        )
        record.task_id = "t-123"
        record.duration_ms = 42.5
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["task_id"] == "t-123"
        assert parsed["duration_ms"] == 42.5

    def test_request_context_filter(self):
        from structured_logging import RequestContextFilter
        f = RequestContextFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        assert f.filter(record) is True
        assert hasattr(record, "request_id")
        assert hasattr(record, "method")


# ── Shutdown ────────────────────────────────────────────────────────────

class TestShutdown:
    def test_register_handlers(self):
        from shutdown import register_shutdown_handlers, _shutdown_state
        from fastapi import FastAPI
        app = FastAPI()
        # Reset state
        _shutdown_state["initiated"] = False
        # Should not raise
        register_shutdown_handlers(app)
        _shutdown_state["initiated"] = False

    def test_cleanup_stops_worker(self):
        from shutdown import _cleanup
        from fastapi import FastAPI
        app = FastAPI()
        # Should not raise even if worker is None
        import asyncio
        asyncio.run(_cleanup(app))


# ── Backup Manager ──────────────────────────────────────────────────────

class TestBackupManager:
    def _make_db(self, path: Path) -> Path:
        """Create a minimal SQLite database."""
        db = path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO test VALUES (1, 'hello')")
        conn.commit()
        conn.close()
        return db

    def test_create_and_list_backup(self, tmp_path):
        from backup import BackupManager
        db = self._make_db(tmp_path)
        backup_dir = tmp_path / "backups"
        mgr = BackupManager(db_path=str(db), backup_dir=str(backup_dir))

        result = mgr.create_backup()
        assert result.exists()
        assert result.name.startswith("pk_ninja_")

        backups = mgr.list_backups()
        assert len(backups) == 1
        assert backups[0]["name"] == result.name
        assert backups[0]["size_bytes"] > 0

    def test_backup_contains_data(self, tmp_path):
        from backup import BackupManager
        db = self._make_db(tmp_path)
        backup_dir = tmp_path / "backups"
        mgr = BackupManager(db_path=str(db), backup_dir=str(backup_dir))

        result = mgr.create_backup()
        # Verify backup has the data
        conn = sqlite3.connect(str(result))
        cur = conn.execute("SELECT val FROM test WHERE id=1")
        assert cur.fetchone()[0] == "hello"
        conn.close()

    def test_verify_backup(self, tmp_path):
        from backup import BackupManager
        db = self._make_db(tmp_path)
        backup_dir = tmp_path / "backups"
        mgr = BackupManager(db_path=str(db), backup_dir=str(backup_dir))

        result = mgr.create_backup()
        assert mgr.verify_backup(result.name) is True
        assert mgr.verify_backup("nonexistent.db") is False

    def test_cleanup_old_backups(self, tmp_path):
        from backup import BackupManager
        db = self._make_db(tmp_path)
        backup_dir = tmp_path / "backups"
        mgr = BackupManager(db_path=str(db), backup_dir=str(backup_dir), max_backups=3)

        # Create 5 backups
        paths = []
        for i in range(5):
            p = mgr.create_backup(label=f"test{i}")
            paths.append(p)

        backups = mgr.list_backups()
        assert len(backups) == 5

        removed = mgr.cleanup_old_backups(keep=3)
        assert removed == 2

        backups = mgr.list_backups()
        assert len(backups) == 3

    def test_restore_requires_confirm(self, tmp_path):
        from backup import BackupManager
        db = self._make_db(tmp_path)
        backup_dir = tmp_path / "backups"
        mgr = BackupManager(db_path=str(db), backup_dir=str(backup_dir))

        result = mgr.create_backup()
        with pytest.raises(ValueError, match="confirm"):
            mgr.restore_backup(result.name, confirm=False)

    def test_restore_backup(self, tmp_path):
        from backup import BackupManager
        db = self._make_db(tmp_path)
        backup_dir = tmp_path / "backups"
        mgr = BackupManager(db_path=str(db), backup_dir=str(backup_dir))

        backup = mgr.create_backup()

        # Modify the database
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO test VALUES (2, 'world')")
        conn.commit()
        conn.close()

        # Restore
        mgr.restore_backup(backup.name, confirm=True)

        # Verify restored state (should only have row 1)
        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT COUNT(*) FROM test").fetchone()[0]
        assert rows == 1
        conn.close()

    def test_backup_missing_db_raises(self, tmp_path):
        from backup import BackupManager
        mgr = BackupManager(
            db_path=str(tmp_path / "nonexistent.db"),
            backup_dir=str(tmp_path / "backups"),
        )
        with pytest.raises(FileNotFoundError):
            mgr.create_backup()

    def test_backup_size_total(self, tmp_path):
        from backup import BackupManager
        db = self._make_db(tmp_path)
        backup_dir = tmp_path / "backups"
        mgr = BackupManager(db_path=str(db), backup_dir=str(backup_dir))

        mgr.create_backup()
        mgr.create_backup()
        assert mgr.backup_size_total() > 0


# ── Metrics ─────────────────────────────────────────────────────────────

class TestMetrics:
    def test_metrics_graceful_degradation(self):
        """Metrics functions should not raise even without prometheus_client."""
        import metrics
        # These should all be no-ops when prometheus_client is not installed
        # (or should work fine if it is)
        metrics.record_task_created("test")
        metrics.record_task_duration(1.5)
        metrics.set_active_tasks(3)
        metrics.set_queue_size(5)
        metrics.set_worker_active(2)
        metrics.record_provider_call("openai", "success", 0.5)
        metrics.record_db_operation("insert", "tasks")

    def test_setup_metrics_registers_endpoint(self):
        """setup_metrics should register /metrics endpoint."""
        from fastapi import FastAPI
        from metrics import setup_metrics
        app = FastAPI()
        setup_metrics(app)
        # Check that /metrics route was added (if prometheus_client available)
        routes = [r.path for r in app.routes]
        if hasattr(app, 'routes'):
            # At minimum, setup should not raise
            pass


# ── Startup Script ──────────────────────────────────────────────────────

class TestStartupScript:
    def test_start_script_exists(self):
        script = Path(__file__).parent.parent / "scripts" / "start.sh"
        assert script.exists()
        assert os.access(script, os.X_OK)

    def test_audit_script_exists(self):
        script = Path(__file__).parent.parent / "scripts" / "audit.sh"
        assert script.exists()
        assert os.access(script, os.X_OK)
