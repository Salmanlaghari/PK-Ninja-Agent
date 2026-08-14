# Changelog

All notable changes to this project will be documented in this file.

## [v0.2.1] - Vercel Deployment Configuration

### Added
- Created `pyproject.toml` to configure the application entrypoint `backend.main:app` using Vercel's recommended modern Python runtime configuration.
- Added `.python-version` file to explicitly pin Python version 3.12 for maximum compatibility and build stability on Vercel.
- Created `DEPLOYMENT.md` containing exhaustive, step-by-step instructions on deploying PK Ninja Agent on Vercel, including configuring `/tmp` writable directories for the SQLite database (`DATABASE_PATH`) and task workspaces (`WORKSPACE_ROOT`).

### Changed
- Updated `README.md` with a new "Vercel Deployment" section referencing `DEPLOYMENT.md`.
- Updated `.env.example` to document supported AI providers (such as Anthropic and Jules) introduced in prior development phases.
