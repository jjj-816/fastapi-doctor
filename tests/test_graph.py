"""最小诊断图的集成测试（假检索器与假 LLM，不依赖 Ollama）。"""

from fastapi_doctor.domain.models import DiagnosisReport, RunStatus
from fastapi_doctor.graph.builder import build_diagnosis_graph
from tests.conftest import CASE_MD, FakeLLM


def invoke_graph(description: str, logs: str = "", retriever=None, llm=None) -> dict:
    return build_diagnosis_graph(retriever=retriever, llm=llm).invoke(
        {
            "run_id": "test-run",
            "description": description,
            "logs": logs,
            "code": "",
            "config": "",
            "status": RunStatus.RUNNING,
        }
    )


def test_complete_input_runs_plan_and_retrieve(make_fake_retriever, fake_llm) -> None:
    retriever = make_fake_retriever({"cases/case-db.md": CASE_MD})
    result = invoke_graph(
        "FastAPI 在容器内访问 PostgreSQL 报错",
        logs="sqlalchemy OperationalError connection refused",
        retriever=retriever,
        llm=fake_llm,
    )

    assert result["status"] == RunStatus.COMPLETED
    assert result["fault_info"].framework == "fastapi"
    assert result["fault_info"].component == "database"
    assert result["clarification_questions"] == []
    assert result["plan"].search_queries
    assert result["evidence"]
    assert result["diagnosis"].most_likely_cause
    assert result["review"].passed is True
    # 官方文档与 Runbook 来源没有语料，不应混入其他类型。
    assert {item.source_type for item in result["evidence"]} == {"incident_case"}


def test_retrieve_with_empty_corpus_returns_no_evidence(
    make_fake_retriever, fake_llm
) -> None:
    retriever = make_fake_retriever(
        {"docs/quickstart.md": "# Quickstart\n\nFastAPI basics. " * 20}
    )
    result = invoke_graph(
        "FastAPI 在容器内访问 PostgreSQL 报错",
        logs="sqlalchemy OperationalError connection refused",
        retriever=retriever,
        llm=fake_llm,
    )

    # 语料里只有官方文档；检索不报错，只是没有案例证据。
    assert {item.source_type for item in result["evidence"]} <= {"official_doc"}
    assert result["diagnosis"] is not None


def test_missing_context_stops_for_clarification() -> None:
    result = invoke_graph("接口出错了")

    assert result["status"] == RunStatus.NEEDS_CLARIFICATION
    assert "plan" not in result
    assert "evidence" not in result
    # 框架是工具前提不再追问，只问组件与日志。
    assert len(result["clarification_questions"]) == 2


TRACEBACK_LOGS = (
    'sqlalchemy.exc.OperationalError: connection to server at "localhost" (::1), '
    "port 5432 failed: Connection refused"
)


def test_traceback_feeds_queries_and_grade(make_fake_retriever, fake_llm) -> None:
    retriever = make_fake_retriever({"cases/case-db.md": CASE_MD})
    result = invoke_graph(
        "FastAPI 在容器内访问 PostgreSQL 报错",
        logs=TRACEBACK_LOGS,
        retriever=retriever,
        llm=fake_llm,
    )

    assert result["fault_info"].exception_type == "OperationalError"
    # 宽泛词 + 异常精确词两条检索词。
    assert len(result["plan"].search_queries) == 2
    assert "OperationalError" in result["plan"].search_queries[1]
    assert result["evidence"][0].doc_id == "case-db"
    assert result["grade"].sufficient is True
    assert result.get("retry_count", 0) == 0
    assert result["status"] == RunStatus.COMPLETED


def test_log_tail_line_feeds_query_without_traceback(
    make_fake_retriever, fake_llm
) -> None:
    retriever = make_fake_retriever({"cases/case-db.md": CASE_MD})
    result = invoke_graph(
        "容器启动正常但宿主机 curl 连接被拒",
        logs="INFO:     Uvicorn running on http://0.0.0.0:8000\n"
        "curl: (7) Failed to connect to localhost port 8000: Connection refused",
        retriever=retriever,
        llm=fake_llm,
    )

    # 无异常栈：宽泛词 + 日志尾行关键词精确词。
    queries = result["plan"].search_queries
    assert len(queries) == 2
    assert "connection" in queries[1] and "refused" in queries[1]


def test_grade_loop_rewrites_up_to_cap(make_fake_retriever, fake_llm) -> None:
    retriever = make_fake_retriever(
        {"docs/quickstart.md": "# Quickstart\n\nFastAPI basics. " * 20}
    )
    result = invoke_graph(
        "FastAPI 在容器内访问 PostgreSQL 报错",
        logs=TRACEBACK_LOGS,
        retriever=retriever,
        llm=fake_llm,
    )

    # 语料中没有任何异常关键字：评分不足，重写两轮后到达上限。
    assert result["grade"].sufficient is False
    assert result["retry_count"] == 2
    # 重写词 = 组件 + 未命中的前两个异常关键字。
    assert result["plan"].search_queries == ["database operationalerror connection"]
    # 证据不足也不阻塞诊断，但结论必须经过审查。
    assert result["status"] == RunStatus.COMPLETED
    assert result["review"].passed is False


def test_review_flags_unknown_citation(make_fake_retriever) -> None:
    retriever = make_fake_retriever({"cases/case-db.md": CASE_MD})
    bad_report = DiagnosisReport(
        most_likely_cause="连接池配置错误",
        confidence=0.9,
        supporting_evidence=["不存在的证据_p0"],
        fix_suggestions=["调大 pool_size"],
    )
    result = invoke_graph(
        "FastAPI 在容器内访问 PostgreSQL 报错",
        logs="sqlalchemy OperationalError connection refused",
        retriever=retriever,
        llm=FakeLLM(report=bad_report),
    )

    assert result["review"].passed is False
    assert any("不存在" in issue for issue in result["review"].issues)
    assert result["status"] == RunStatus.COMPLETED


def test_review_flags_dangerous_command(make_fake_retriever) -> None:
    retriever = make_fake_retriever({"cases/case-db.md": CASE_MD})
    danger_report = DiagnosisReport(
        most_likely_cause="数据卷损坏",
        confidence=0.7,
        supporting_evidence=["case-db_p0"],
        fix_suggestions=["执行 docker volume rm db_data 后重建"],
    )
    result = invoke_graph(
        "FastAPI 在容器内访问 PostgreSQL 报错",
        logs="sqlalchemy OperationalError connection refused",
        retriever=retriever,
        llm=FakeLLM(report=danger_report),
    )

    assert result["review"].needs_confirmation is True
    assert result["review"].dangerous_commands == [
        "执行 docker volume rm db_data 后重建"
    ]
    assert result["status"] == RunStatus.NEEDS_CONFIRMATION
