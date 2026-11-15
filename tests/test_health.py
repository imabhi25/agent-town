from fastapi.testclient import TestClient

from app.main import app


def test_health():
    # No `with` block, so the lifespan (database pool) is not started.
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}


def test_cors_allows_only_the_configured_origin():
    client = TestClient(app)
    ok = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    other = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
