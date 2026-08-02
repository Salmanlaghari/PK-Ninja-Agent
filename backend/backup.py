"""Database backup and recovery for PK Ninja Agent.

Provides scheduled and on-demand SQLite backup with rotation.

Usage:
    from backup import BackupManager
    mgr = BackupManager(db_path="./pk_ninja.db", backup_dir="./backups")
    mgr.create_backup()
    mgr.list_backups()
    mgr.restore_backup("pk_ninja_20260802_120000.db")
    mgr.cleanup_old_backups(keep=7)
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("pk_ninja.backup")


class BackupManager:
    """Manages SQLite database backups with rotation."""

    def __init__(
        self,
        db_path: str = "./pk_ninja.db",
        backup_dir: str = "./backups",
        max_backups: int = 30,
    ):
        self.db_path = Path(db_path).resolve()
        self.backup_dir = Path(backup_dir).resolve()
        self.max_backups = max_backups
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(self, label: Optional[str] = None) -> Path:
        """Create a point-in-time backup of the database.

        Uses SQLite's online backup API for a consistent snapshot even
        while the application is running.

        Args:
            label: Optional label for the backup (e.g. "pre-migration").

        Returns:
            Path to the created backup file.
        """
        if not self.db_path.exists():
            log.warning("Database not found at %s — nothing to back up", self.db_path)
            raise FileNotFoundError(f"Database not found: {self.db_path}")

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        suffix = f"_{label}" if label else ""
        backup_name = f"pk_ninja_{ts}{suffix}.db"
        backup_path = self.backup_dir / backup_name

        try:
            # Use SQLite online backup API (safe for concurrent reads).
            # Both connections use the centralized serverless-safe connector
            # (busy_timeout + temp_store=MEMORY) so a concurrent writer can't
            # make the backup fail with "database is locked".
            from db import connect_sync as _db_connect_sync
            source = _db_connect_sync(self.db_path)
            dest = sqlite3.connect(str(backup_path), timeout=30.0)
            source.backup(dest)
            source.close()
            dest.close()
            log.info("Backup created: %s (%.1f KB)",
                     backup_path.name, backup_path.stat().st_size / 1024)
            return backup_path
        except Exception as exc:
            log.error("Backup failed: %s", exc)
            # Clean up partial backup
            if backup_path.exists():
                backup_path.unlink()
            raise

    def list_backups(self) -> List[Dict[str, Any]]:
        """List all available backups, newest first."""
        backups = []
        for f in sorted(self.backup_dir.glob("pk_ninja_*.db"), reverse=True):
            stat = f.stat()
            backups.append({
                "name": f.name,
                "path": str(f),
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
        return backups

    def restore_backup(self, backup_name: str, confirm: bool = False) -> Path:
        """Restore the database from a backup.

        Args:
            backup_name: Name of the backup file (e.g. "pk_ninja_20260802_120000.db").
            confirm: Must be True to actually restore (safety guard).

        Returns:
            Path to the restored database.

        Raises:
            FileNotFoundError: If backup doesn't exist.
            ValueError: If confirm is not True.
        """
        if not confirm:
            raise ValueError("Set confirm=True to restore (destructive operation)")

        backup_path = self.backup_dir / backup_name
        if not backup_path.exists():
            raise FileNotFoundError(f"Backup not found: {backup_path}")

        # Create a safety backup of the current DB before restoring
        if self.db_path.exists():
            safety = self.create_backup(label="pre-restore")
            log.info("Safety backup before restore: %s", safety.name)

        shutil.copy2(backup_path, self.db_path)
        log.info("Database restored from %s", backup_name)
        return self.db_path

    def cleanup_old_backups(self, keep: Optional[int] = None) -> int:
        """Remove old backups beyond the retention limit.

        Args:
            keep: Number of recent backups to keep. Defaults to self.max_backups.

        Returns:
            Number of backups removed.
        """
        keep = keep or self.max_backups
        backups = sorted(self.backup_dir.glob("pk_ninja_*.db"))
        to_remove = backups[:-keep] if len(backups) > keep else []

        removed = 0
        for f in to_remove:
            try:
                f.unlink()
                log.info("Removed old backup: %s", f.name)
                removed += 1
            except Exception as exc:
                log.warning("Failed to remove backup %s: %s", f.name, exc)

        if removed:
            log.info("Cleanup: removed %d old backup(s), keeping %d", removed, keep)
        return removed

    def verify_backup(self, backup_name: str) -> bool:
        """Verify that a backup file is a valid SQLite database.

        Args:
            backup_name: Name of the backup file.

        Returns:
            True if the backup is valid.
        """
        backup_path = self.backup_dir / backup_name
        if not backup_path.exists():
            return False

        try:
            conn = sqlite3.connect(str(backup_path))
            cur = conn.execute("PRAGMA integrity_check")
            result = cur.fetchone()[0]
            conn.close()
            return result == "ok"
        except Exception:
            return False

    def backup_size_total(self) -> int:
        """Return total size of all backups in bytes."""
        return sum(f.stat().st_size for f in self.backup_dir.glob("pk_ninja_*.db"))


def schedule_backups(
    db_path: str = "./pk_ninja.db",
    backup_dir: str = "./backups",
    interval_hours: int = 24,
    max_backups: int = 30,
) -> None:
    """Set up periodic background backups (to be called from startup).

    This creates a daemon thread that runs backups at the specified interval.
    """
    import threading

    mgr = BackupManager(db_path, backup_dir, max_backups)

    def _backup_loop():
        import time
        while True:
            try:
                mgr.create_backup()
                mgr.cleanup_old_backups(max_backups)
            except Exception as exc:
                log.error("Scheduled backup failed: %s", exc)
            time.sleep(interval_hours * 3600)

    t = threading.Thread(target=_backup_loop, daemon=True, name="backup-worker")
    t.start()
    log.info("Scheduled backups every %dh (keeping %d)", interval_hours, max_backups)
