# Security Policy — PK Ninja Agent

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.0.x | ✅ Active |
| 0.9.x | ✅ Security fixes |
| < 0.9 | ❌ End of life |

## Reporting a Vulnerability

If you discover a security vulnerability in PK Ninja Agent, please report it responsibly:

1. **Do NOT** open a public GitHub issue.
2. Email the maintainer privately or use GitHub's private vulnerability reporting.
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We aim to acknowledge reports within 48 hours and provide a fix within 7 days for critical issues.

## Security Architecture

### Authentication

- **Opt-in by default** (`AUTH_ENABLED=false`). When disabled, all requests are anonymous.
- **GitHub token verification** against GitHub's `/user` endpoint (not stored server-side).
- **Guest mode** with ephemeral 4-hour TTL sessions.
- **HMAC-SHA256 signed** stateless session tokens (base64-encoded).
- Session tokens never exposed in API responses.

### Command Execution Sandbox

- All terminal commands run with `cwd` locked to the workspace directory.
- **Allowlist**: Only approved programs may execute (ls, cat, git, python3, node, etc.).
- **Blocklist**: Destructive patterns blocked (rm -rf /, fork bombs, dd to devices, etc.).
- **Path containment**: Commands referencing absolute paths outside the workspace or parent traversal (`../`) are rejected.
- **Shell operator control**: Unquoted `|`, `&&`, `||`, `;`, `&`, backticks, `$()` are blocked outside quotes.
- **Timeout enforcement**: Commands are killed after configurable timeout (default 30s).
- **Process group isolation**: Each command runs in its own session (`start_new_session=True`).

### File System Security

- **Path traversal protection**: All file operations go through `Workspace.safe_path()` which rejects `..` traversal.
- **Workspace containment**: Workspaces are rooted under `WORKSPACE_ROOT`; symlinks escaping the root are detected and blocked.
- **Sensitive file protection**: `.env`, SSH keys, certificates (`.pem`, `.key`, `.p12`, `.pfx`), and credential files are flagged.
- **World-writable directory detection**: Workspace validation checks for insecure directory permissions.

### Security Hardening (opt-in, `SECURITY_HARDENING_ENABLED=true`)

- **Enhanced command validation**: 15+ additional blocklist patterns (`rm -rf ~`, `chmod -R 777`, `cat /etc/shadow`, `nc` listener, `crontab`, `systemctl`, etc.).
- **Destructive argument containment**: Blocks `rm -rf .`, `rm -rf *`, parent traversal in `rm`/`mv`/`cp`/`rmdir` arguments.
- **Workspace validation API**: `GET /api/security/workspace/{name}` checks for symlink escapes, world-writable dirs, file count limits.
- **Command dry-run API**: `POST /api/security/check-command` validates commands without execution.

### Data Protection

- **No secrets in API responses**: Every endpoint is tested to ensure API keys, tokens, passwords, and secrets are never serialized.
- **Server-side only**: GitHub tokens, AI API keys, and auth secrets stay in server-side settings.
- **SQLite WAL mode**: Database uses Write-Ahead Logging for concurrent read safety.
- **Backup encryption**: Backup files are plain SQLite; encrypt at rest using filesystem-level encryption or external tools.

### Git Operations

- **Sandboxed git**: All git commands run with `cwd` locked to the workspace.
- **Credential isolation**: `GIT_TERMINAL_PROMPT=0` prevents interactive credential prompts.
- **No credential storage**: Git credentials are not stored in workspaces.

### Network Security

- **Reverse proxy ready**: nginx.conf provided with WebSocket support and API proxying.
- **CORS**: Configure as needed for your deployment.
- **Rate limiting**: Not yet implemented; use reverse proxy rate limiting (nginx, Cloudflare) in production.

## Production Security Checklist

- [ ] Set `APP_ENV=production`
- [ ] Set `AUTH_ENABLED=true` and configure `AUTH_SECRET`
- [ ] Set `DEBUG=false`
- [ ] Run `./scripts/audit.sh` before deploying
- [ ] Enable `SECURITY_HARDENING_ENABLED=true` for untrusted environments
- [ ] Set up TLS termination (nginx, Cloudflare, etc.)
- [ ] Configure firewall to restrict access
- [ ] Set up log monitoring and alerting
- [ ] Run regular dependency audits (`pip-audit`)
- [ ] Back up the database regularly
- [ ] Review workspace permissions
- [ ] Test disaster recovery procedures

## Known Limitations

- **Rate limiting**: Not built-in; rely on reverse proxy.
- **CSRF protection**: Not yet implemented for cookie-based auth.
- **Multi-tenancy**: Workspace isolation is filesystem-based, not container-based.
- **Secrets at rest**: Database and backups are not encrypted; use filesystem encryption.

## Security Audit Tools

```bash
# Run the full security audit
./scripts/audit.sh

# Dependency vulnerability scan
pip-audit --strict --desc

# Static analysis
bandit -r backend/ agents/ providers/ -c pyproject.toml

# Check for hardcoded secrets
grep -rn 'ghp_\|sk-\|AKIA\|password.*=' backend/ --include="*.py"
```
