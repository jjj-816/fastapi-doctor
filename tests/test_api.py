"""HTTP 接口测试（设计 §8.1）：注入假检索器与假 LLM，不依赖真实索引。

覆盖：创建任务、轮询状态、SSE 事件顺序、澄清恢复、危险确认、反馈。
"""

import time

import pytest
from fastapi.testclient import TestClient

from fastapi_doctor import api
from fastapi_doctor.domain.models import DiagnosisReport
from fastapi_doctor.graph.builder import build_diagnosis_graph
from tests.conftest import CASE_MD, FakeLLM

DANGER_REPORT = DiagnosisReport(
    most_likely_cause="数据卷损坏",
    confidence=0.7,
    supporting_evidence=["case-db_p0"],
    fix_suggestions=["执行 docker volume rm db_data 后重建"],
)


def make_client(monkeypatch, tmp_path, make_fake_retriever, llm: FakeLLM):
    retriever = make_fake_retriever({"cases/case-db.md": CASE_MD})
    monkeypatch.setattr(
        api,
        "_build_graph",
        lambda: build_diagnosis_graph(retriever=retriever, llm=llm),
    )
    monkeypatch.setattr(
        "fastapi_doctor.config.APPLICATION_DB_PATH", tmp_path / "application.db"
    )
    return TestClient(api.app)


def wait_for_status(client: TestClient, run_id: str, *statuses: str, timeout: float = 10.0):
    """轮询运行状态直至进入给定状态之一（后台任务跑在应用事件循环里）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = client.get(f"/api/runs/{run_id}").json()
        if snapshot["status"] in statuses:
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"等待状态超时: {snapshot['status']}")


def test_health() -> None:
    with TestClient(api.app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_run_completes_and_returns_result(monkeypatch, tmp_path, make_fake_retriever) -> None:
    client = make_client(monkeypatch, tmp_path, make_fake_retriever, FakeLLM())
    with client:
        created = client.post(
            "/api/runs",
            json={
                "description": "FastAPI 在容器内访问 PostgreSQL 报错",
                "logs": "sqlalchemy OperationalError connection refused",
            },
        ).json()

        assert created["status"] == "running"
        snapshot = wait_for_status(client, created["run_id"], "completed")

    assert snapshot["result"]["diagnosis"]["most_likely_cause"]
    assert snapshot["result"]["review"]["passed"] is True
    assert snapshot["error"] is None


def test_sse_streams_event_sequence(monkeypatch, tmp_path, make_fake_retriever) -> None:
    client = make_client(monkeypatch, tmp_path, make_fake_retriever, FakeLLM())
    with client:
        run_id = client.post(
            "/api/runs",
            json={"description": "FastAPI 在容器内访问 PostgreSQL 报错",
                  "logs": "sqlalchemy OperationalError connection refused"},
        ).json()["run_id"]

        events: list[str] = []
        with client.stream("GET", f"/api/runs/{run_id}/events") as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = 0
            for line in response.iter_lines():
                lines += 1
                if line.startswith("event: "):
                    events.append(line[len("event: "):])
                if lines > 2000:  # 防御：异常情况下不至于无限读
                    break

    assert events[0] == "run_started"
    assert "input_analyzed" in events
    assert "evidence_found" in events
    assert "diagnosis_generated" in events
    assert events[-1] == "run_completed"


def test_clarification_pause_and_resume(monkeypatch, tmp_path, make_fake_retriever) -> None:
    client = make_client(monkeypatch, tmp_path, make_fake_retriever, FakeLLM())
    with client:
        run_id = client.post(
            "/api/runs", json={"description": "接口出错了"}
        ).json()["run_id"]
        snapshot = wait_for_status(
            client, run_id, "waiting_clarification", "completed", timeout=5.0
        )

        if snapshot["status"] == "waiting_clarification":
            resumed = client.post(
                f"/api/runs/{run_id}/resume",
                json={"answers": {"logs": "sqlalchemy OperationalError connection refused"}},
            )
            assert resumed.status_code == 200
            snapshot = wait_for_status(client, run_id, "completed", "failed")

    assert snapshot["status"] == "completed"


def test_dangerous_confirmation_flow(monkeypatch, tmp_path, make_fake_retriever) -> None:
    client = make_client(monkeypatch, tmp_path, make_fake_retriever, FakeLLM(DANGER_REPORT))
    with client:
        run_id = client.post(
            "/api/runs",
            json={"description": "FastAPI 在容器内访问 PostgreSQL 报错",
                  "logs": "sqlalchemy OperationalError connection refused"},
        ).json()["run_id"]
        snapshot = wait_for_status(client, run_id, "waiting_confirmation")

        # 缺少 approved 字段应被拒绝。
        assert client.post(f"/api/runs/{run_id}/resume", json={}).status_code == 422

        # 批准后正常完成。
        assert client.post(
            f"/api/runs/{run_id}/resume", json={"approved": True}
        ).status_code == 200
        snapshot = wait_for_status(client, run_id, "completed", "failed")

    assert snapshot["status"] == "completed"
    assert snapshot["result"]["review"]["confirmed"] is True


def test_dangerous_rejection_fails_run(monkeypatch, tmp_path, make_fake_retriever) -> None:
    client = make_client(monkeypatch, tmp_path, make_fake_retriever, FakeLLM(DANGER_REPORT))
    with client:
        run_id = client.post(
            "/api/runs",
            json={"description": "FastAPI 在容器内访问 PostgreSQL 报错",
                  "logs": "sqlalchemy OperationalError connection refused"},
        ).json()["run_id"]
        wait_for_status(client, run_id, "waiting_confirmation")

        client.post(f"/api/runs/{run_id}/resume", json={"approved": False})
        snapshot = wait_for_status(client, run_id, "completed", "failed")

    assert snapshot["status"] == "failed"
    assert "拒绝" in (snapshot["error"] or "")


def test_feedback_after_completion(monkeypatch, tmp_path, make_fake_retriever) -> None:
    client = make_client(monkeypatch, tmp_path, make_fake_retriever, FakeLLM())
    with client:
        run_id = client.post(
            "/api/runs",
            json={"description": "FastAPI 在容器内访问 PostgreSQL 报错",
                  "logs": "sqlalchemy OperationalError connection refused"},
        ).json()["run_id"]
        wait_for_status(client, run_id, "completed")

        response = client.post(
            f"/api/runs/{run_id}/feedback",
            json={"rating": 5, "root_cause": "容器内误用 localhost", "solution": "改用服务名"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_unknown_run_returns_404(monkeypatch, tmp_path, make_fake_retriever) -> None:
    client = make_client(monkeypatch, tmp_path, make_fake_retriever, FakeLLM())
    with client:
        assert client.get("/api/runs/does-not-exist").status_code == 404
        assert client.get("/api/runs/does-not-exist/events").status_code == 404
        assert client.post(
            "/api/runs/does-not-exist/feedback", json={"rating": 3}
        ).status_code == 404


def test_resume_rejects_non_waiting_run(monkeypatch, tmp_path, make_fake_retriever) -> None:
    client = make_client(monkeypatch, tmp_path, make_fake_retriever, FakeLLM())
    with client:
        run_id = client.post(
            "/api/runs",
            json={"description": "FastAPI 在容器内访问 PostgreSQL 报错",
                  "logs": "sqlalchemy OperationalError connection refused"},
        ).json()["run_id"]
        wait_for_status(client, run_id, "completed")

        response = client.post(
            f"/api/runs/{run_id}/resume", json={"answers": {"logs": "x"}}
        )

    assert response.status_code == 409
