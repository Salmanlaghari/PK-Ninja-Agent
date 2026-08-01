# Contributing to PK Ninja Agent

Thank you for your interest in contributing to PK Ninja Agent! This document outlines how to set up your development environment, the conventions we follow, and the process for submitting changes.

---

## Getting Started

### Prerequisites

- Python 3.11+
- Git
- A GitHub account (for PRs)
- Optional: a GitHub personal access token and/or an AI provider API key (for testing provider integration)

### Local Development Setup

1. **Clone the repository:**

   ```bash
   gh repo clone Salmanlaghari/PK-Ninja-Agent
   cd PK-Ninja-Agent
   ```

2. **Install Python dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

   Key dependencies: `fastapi`, `uvicorn`, `httpx`, `aiosqlite`, `pydantic`, `python-dotenv`.

3. **Configure environment:**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` with your GitHub token, repo, and AI provider settings. See the README for the full list of environment variables. All v0.7.0 features are opt-in and disabled by default, so you can start with a minimal config.

4. **Run the test suite:**

   ```bash
   python -m pytest -q
   ```

   All tests should pass. If any fail, fix them before making changes.

5. **Start the development server:**

   ```bash
   python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
   ```

   Open `http://localhost:8000` in your browser.

---

## Development Conventions

### Architecture principles

PK Ninja Agent follows a strict layering and backward-compatibility discipline. When contributing, please respect these principles:

- **Do not rebuild.** New features are layered on top of the existing stable codebase. Never replace an existing module's architecture or remove working functionality without explicit discussion.
- **Backward compatible by default.** New features must be opt-in via configuration flags (environment variables), with defaults that preserve existing behavior. Every existing test must continue to pass unchanged.
- **Modular.** Each major feature lives in its own module (`backend/auth.py`, `backend/settings_store.py`, `backend/workspace_manager.py`, `backend/release_checks.py`, etc.) with a narrow public interface.
- **No secrets in responses.** Never serialize API keys, tokens, passwords, or secrets into any API response. The test suite includes a secret-leak guard that checks for `api_key`, `token`, `password`, and `secret` substrings in response bodies.

### Code style

- Python: follow PEP 8. Use type hints (`from __future__ import annotations` is used in most modules). Keep functions focused and well-documented with docstrings.
- Frontend: vanilla JavaScript (no build step, no framework). Use IIFE modules for new features (see the `Auth`, `Settings`, `Workspaces`, `Providers`, and `Dashboard` modules in `frontend/app.js` for the pattern).
- CSS: extend `frontend/style.css` with new rules grouped under a comment header per feature.
- Logging: use the `logging` module (`log = logging.getLogger("pk_ninja.<module>")`). Never print secrets to logs.

### Testing

Testing is mandatory for all new features. The project uses `pytest` with `fastapi.testclient.TestClient`.

- **Write tests first or alongside your feature.** Every new endpoint, module function, and validation rule should have test coverage.
- **Use the `_build_client()` pattern.** New test files should use a helper that clears the settings cache, resets relevant services, and `importlib.reload(main)` so each test gets a fresh app with the correct environment. See `tests/test_auth.py`, `tests/test_workspace_manager.py`, and `tests/test_dashboard.py` for examples.
- **Cover the happy path AND error cases.** Test validation failures (400), missing resources, and auth compatibility (both disabled and enabled modes).
- **Add secret-leak guard tests.** For any new endpoint, add a test that checks the response body does not contain `api_key`, `token`, `password`, or `secret` substrings.
- **Add auth-compat tests.** If your feature has auth-protected endpoints, test that they return 401 when `AUTH_ENABLED=true` without a token, and work with a guest token.
- **Run the full suite before committing:**

  ```bash
  python -m pytest -q
  ```

  All tests must pass.

### Commit messages

Use conventional commit format:

```
type(scope): short description

- bullet point of key change
- another change

Test count: NNN
```

Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`. Common scopes: `auth`, `settings`, `workspace-manager`, `dashboard`, `release-prep`, `provider`, `agent`, etc.

Examples:
- `feat(auth): modular authentication system (v0.7.0 phase 1)`
- `fix(workspace-manager): correct _safe_name length check`
- `docs: add CHANGELOG and ROADMAP`

---

## Pull Request Process

1. **Create a feature branch** off `main`:

   ```bash
   git checkout main
   git pull
   git checkout -b feat/your-feature-name
   ```

2. **Make your changes** following the conventions above. Test after every major feature.

3. **Run the full test suite** and ensure all tests pass:

   ```bash
   python -m pytest -q
   ```

4. **Update documentation** if your change adds new endpoints, environment variables, or features:
   - Add an entry to `CHANGELOG.md` under an unreleased section or the next version.
   - Update `README.md` if there are new setup steps, environment variables, or user-facing features.
   - Update `ROADMAP.md` if a planned item is now complete.

5. **Commit with clear, descriptive messages** (see Commit messages above).

6. **Push your branch:**

   ```bash
   git push https://x-access-token:$GITHUB_TOKEN@github.com/Salmanlaghari/PK-Ninja-Agent.git feat/your-feature-name
   ```

7. **Create a pull request** using the GitHub CLI:

   ```bash
   gh pr create --title "feat: your feature" --body "Description of changes, test results, and backward-compatibility notes"
   ```

8. **Ensure CI passes** (if configured) and respond to review feedback.

9. **Do not merge your own PR** unless you have explicit approval from a maintainer.

---

## Adding a New AI Provider

PK Ninja Agent uses a pluggable provider system (v0.6.0). To add a new provider:

1. Create `providers/myprovider_provider.py` implementing `ProviderProtocol` (see `providers/openai_provider.py` for the adapter pattern).
2. Register it in `providers/__init__.py` via `register_adapter()`.
3. Add any needed settings (API key, base URL) to `backend/config.py` `Settings`.
4. Enable it via `PROVIDER_ENABLED` or the `/api/providers/enable` endpoint.
5. Add tests using the `MockProvider`/`MockConfig` pattern for deterministic behavior.
6. Update the README provider table and CHANGELOG.

No changes to `agent.py`, `workspace.py`, `terminal.py`, `github.py`, or the event bus are required — the tool and safety layers are provider-independent.

---

## Reporting Issues

When reporting a bug, please include:

- PK Ninja Agent version (check `/api/system/health` or `/health`)
- Python version and OS
- Steps to reproduce
- Expected vs. actual behavior
- Relevant log output (never include secrets/tokens)
- Your configuration (with secrets redacted)

For feature requests, describe the use case and check `ROADMAP.md` to see if it is already planned.

---

## Code of Conduct

Be respectful and constructive in all interactions. We welcome contributors of all experience levels. Personal attacks, harassment, and discriminatory behavior will not be tolerated.

Thank you for contributing to PK Ninja Agent!
