"""API health endpoint and basic endpoint contract."""
from fastapi.testclient import TestClient


def test_health_returns_ok():
    from backend.main import app
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        # v2 reports its version for diagnostics.
        assert "version" in body
        assert body["version"] != ""


def test_index_html_served():
    from backend.main import app
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "NinjaDev" in r.text or "NINJA" in r.text or "PK" in r.text


def test_static_css_served():
    from backend.main import app
    with TestClient(app) as c:
        r = c.get("/static/style.css")
        assert r.status_code == 200
        assert "--accent" in r.text


def test_create_task_validation():
    from backend.main import app
    with TestClient(app) as c:
        # Empty description rejected by pydantic.
        r = c.post("/api/tasks", json={"description": ""})
        assert r.status_code == 422


def test_diff_unknown_task_404():
    from backend.main import app
    with TestClient(app) as c:
        r = c.get("/api/diff?task_id=does-not-exist")
        assert r.status_code == 404


def test_openapi_available():
    from backend.main import app
    with TestClient(app) as c:
        r = c.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json()["paths"]
        assert "/health" in paths
        assert "/api/tasks" in paths
