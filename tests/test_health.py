from fastapi.testclient import TestClient

from app.main import app


def test_health():
    # No `with` block, so the lifespan (database pool) is not started.
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
