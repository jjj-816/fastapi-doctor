"""HTTP 接口测试：注入假检索器，不依赖 Ollama 与真实索引。"""

from fastapi.testclient import TestClient

from fastapi_doctor import api
from fastapi_doctor.graph.builder import build_diagnosis_graph
from tests.conftest import CASE_MD


client = TestClient(api.app)


def test_health() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_diagnose_returns_retrieved_evidence(monkeypatch, make_fake_retriever) -> None:
    retriever = make_fake_retriever({"cases/case-db.md": CASE_MD})
    monkeypatch.setattr(
        api, "graph", build_diagnosis_graph(retriever=retriever)
    )

    response = client.post(
        "/api/diagnose",
        json={
            "description": "FastAPI 在容器内访问 PostgreSQL 报错",
            "logs": "sqlalchemy OperationalError connection refused",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "retrieved"
    assert payload["fault_info"]["component"] == "database"
    assert payload["plan"] is not None
    assert payload["evidence"]
    assert payload["evidence"][0]["doc_id"] == "case-db"
    assert payload["grade"]["sufficient"] is True
