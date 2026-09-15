from fastapi.testclient import TestClient

from fastapi_doctor.api import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_diagnose_returns_graph_result() -> None:
    response = client.post(
        "/api/diagnose",
        json={
            "description": "FastAPI returns 500 while accessing PostgreSQL",
            "logs": "500 sqlalchemy connection refused",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "planned"
    assert payload["fault_info"]["component"] == "database"
    assert payload["plan"] is not None
