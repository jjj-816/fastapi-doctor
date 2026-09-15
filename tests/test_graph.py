"""最小诊断图的集成测试（假检索器，不依赖 Ollama）。"""

from fastapi_doctor.domain.models import Evidence, RunStatus
from fastapi_doctor.graph.builder import build_diagnosis_graph
from tests.conftest import CASE_MD


def invoke_graph(description: str, logs: str = "", retriever=None) -> dict:
    return build_diagnosis_graph(retriever=retriever).invoke(
        {
            "run_id": "test-run",
            "description": description,
            "logs": logs,
            "code": "",
            "config": "",
            "status": RunStatus.RUNNING,
        }
    )


def test_complete_input_runs_plan_and_retrieve(make_fake_retriever) -> None:
    retriever = make_fake_retriever({"cases/case-db.md": CASE_MD})
    result = invoke_graph(
        "FastAPI 在容器内访问 PostgreSQL 报错",
        logs="sqlalchemy OperationalError connection refused",
        retriever=retriever,
    )

    assert result["status"] == RunStatus.RETRIEVED
    assert result["fault_info"].framework == "fastapi"
    assert result["fault_info"].component == "database"
    assert result["clarification_questions"] == []
    assert result["plan"].search_queries
    assert result["evidence"]
    evidence = result["evidence"]
    assert all(isinstance(item, Evidence) for item in evidence)
    assert evidence[0].doc_id == "case-db"
    assert evidence[0].source_type == "incident_case"
    # 官方文档与 Runbook 来源没有语料，不应混入其他类型。
    assert {item.source_type for item in evidence} == {"incident_case"}


def test_retrieve_with_empty_corpus_returns_no_evidence(make_fake_retriever) -> None:
    retriever = make_fake_retriever({"docs/quickstart.md": "# Quickstart\n\nFastAPI basics. " * 20})
    result = invoke_graph(
        "FastAPI 在容器内访问 PostgreSQL 报错",
        logs="sqlalchemy OperationalError connection refused",
        retriever=retriever,
    )

    assert result["status"] == RunStatus.RETRIEVED
    # 语料里只有官方文档；检索不报错，只是没有案例证据。
    assert {item.source_type for item in result["evidence"]} <= {"official_doc"}


def test_missing_context_stops_for_clarification() -> None:
    result = invoke_graph("接口出错了")

    assert result["status"] == RunStatus.NEEDS_CLARIFICATION
    assert "plan" not in result
    assert "evidence" not in result
    assert len(result["clarification_questions"]) == 3


TRACEBACK_LOGS = (
    'sqlalchemy.exc.OperationalError: connection to server at "localhost" (::1), '
    "port 5432 failed: Connection refused"
)


def test_traceback_feeds_queries_and_grade(make_fake_retriever) -> None:
    retriever = make_fake_retriever({"cases/case-db.md": CASE_MD})
    result = invoke_graph(
        "FastAPI 在容器内访问 PostgreSQL 报错",
        logs=TRACEBACK_LOGS,
        retriever=retriever,
    )

    assert result["status"] == RunStatus.RETRIEVED
    assert result["fault_info"].exception_type == "OperationalError"
    # 宽泛词 + 异常精确词两条检索词。
    assert len(result["plan"].search_queries) == 2
    assert "OperationalError" in result["plan"].search_queries[1]
    assert result["evidence"][0].doc_id == "case-db"
    assert result["grade"].sufficient is True
    assert result.get("retry_count", 0) == 0


def test_grade_loop_rewrites_up_to_cap(make_fake_retriever) -> None:
    retriever = make_fake_retriever(
        {"docs/quickstart.md": "# Quickstart\n\nFastAPI basics. " * 20}
    )
    result = invoke_graph(
        "FastAPI 在容器内访问 PostgreSQL 报错",
        logs=TRACEBACK_LOGS,
        retriever=retriever,
    )

    assert result["status"] == RunStatus.RETRIEVED
    # 语料中没有任何异常关键字：评分不足，重写两轮后到达上限并停止。
    assert result["grade"].sufficient is False
    assert result["retry_count"] == 2
    # 重写词 = 组件 + 未命中的前两个异常关键字。
    assert result["plan"].search_queries == ["database operationalerror connection"]
