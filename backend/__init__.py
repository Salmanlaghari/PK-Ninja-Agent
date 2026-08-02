"""PK Ninja Agent — backend package.

This file marks ``backend`` as a Python package so that Vercel's modern Python
runtime can resolve the ``backend.main:app`` entrypoint declared in
``pyproject.toml`` (``[tool.vercel] entrypoint = "backend.main:app"``).

It is intentionally minimal: ``backend/main.py`` still inserts the ``backend/``
directory onto ``sys.path`` at import time so that the sibling modules
(``agent``, ``config``, ``ai_provider``, …) remain importable via their bare
names, preserving compatibility with both the package form
(``uvicorn backend.main:app``) and the script form
(``uvicorn main:app`` from inside ``backend/``).
"""
