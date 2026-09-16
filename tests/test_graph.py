"""诊断图集成测试（假检索器与假 LLM，不依赖 Ollama）。

覆盖四条路径（设计 §8.1）：完整诊断、澄清暂停与恢复、证据不足重试、
危险建议确认（批准/拒绝）。
"""

from langgraph.types import Command

from fastapi_doctor.domain.models import (
    DiagnosisReport,
    Evidence,
    EvidenceGrade,
    InvestigationPlan,
    RunStatus,
)
from fastapi_doctor.graph.builder import build_diagnosis_graph
from fastapi_doctor.graph.nodes import CLARIFY_SKIPPED_RESUME, make_retrieve_node
from tests.conftest import CASE_MD, FakeLLM

TRACEBACK_LOGS = (
    'sqlalchemy.exc.OperationalError: connection to server at "localhost" (::1), '
    "port 5432 failed: Connection refused"
)

INPUT = {
    "run_id": "test-run",
    "description": "FastAPI 在容器内访问 PostgreSQL 报错",
    "logs": "",
    "code": "",
    "config": "",
    "status": RunStatus.RUNNING,
}
CONFIG = {"configurable": {"thread_id": "test-thread"}}


def build(retriever=None, llm=None):
    return build_diagnosis_graph(retriever=retriever, llm=llm)


def test_complete_input_runs_full_pipeline(make_fake_retriever, fake_llm) -> None:
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}), llm=fake_llm
    )
    result = graph.invoke(
        {**INPUT, "logs": "sqlalchemy OperationalError connection refused"}, CONFIG
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
    graph = build(
        retriever=make_fake_retriever(
            {"docs/quickstart.md": "# Quickstart\n\nFastAPI basics. " * 20}
        ),
        llm=fake_llm,
    )
    result = graph.invoke(
        {**INPUT, "logs": "sqlalchemy OperationalError connection refused"}, CONFIG
    )

    # 语料里只有官方文档；检索不报错，只是没有案例证据。
    assert {item.source_type for item in result["evidence"]} <= {"official_doc"}
    assert result["diagnosis"] is not None


def test_missing_context_pauses_for_clarification(make_fake_retriever) -> None:
    graph = build()
    result = graph.invoke({**INPUT, "description": "接口出错了"}, CONFIG)

    assert "__interrupt__" in result
    value = result["__interrupt__"][0].value
    assert value["type"] == "clarification"
    # 框架是工具前提不再追问，只问组件与日志。
    assert len(value["questions"]) == 2
    # missing 给出待补字段键名，前端据此映射 resume 的 answers。
    assert value["missing"] == ["component", "logs"]
    assert "diagnosis" not in result


def test_clarification_resume_reanalyzes_and_completes(
    make_fake_retriever, fake_llm
) -> None:
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}), llm=fake_llm
    )
    graph.invoke({**INPUT, "description": "接口出错了"}, CONFIG)
    resumed = graph.invoke(
        Command(resume={"logs": "sqlalchemy OperationalError connection refused"}),
        CONFIG,
    )

    assert resumed["status"] == RunStatus.COMPLETED
    assert resumed["clarify_rounds"] == 1
    assert resumed["fault_info"].component == "database"
    # 第二轮经过澄清节点（信息已补齐）不得清空第一轮记录的问题。
    assert len(resumed["clarification_questions"]) == 2


def test_component_answer_is_kept_and_unanswered_fields_skipped(
    make_fake_retriever, fake_llm
) -> None:
    """用户只回答阶段、留空日志：回答必须生效，留空项不再重复追问。"""
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}), llm=fake_llm
    )
    graph.invoke({**INPUT, "description": "接口出错了"}, CONFIG)
    resumed = graph.invoke(Command(resume={"component": "请求处理"}), CONFIG)

    assert resumed["status"] == RunStatus.COMPLETED
    assert resumed["clarify_rounds"] == 1  # 没有把同样的问题再问一轮
    assert resumed["fault_info"].component == "请求处理"  # 用户回答优先于关键词推断
    assert resumed["clarify_declined"] == ["logs"]


def test_explanation_question_runs_without_clarification(
    make_fake_retriever, fake_llm
) -> None:
    """解释型提问（为什么/如何）不追问 traceback，凭描述直接检索诊断。"""
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}), llm=fake_llm
    )
    result = graph.invoke(
        {
            **INPUT,
            "description": (
                '我在 FastAPI 中用 @app.middleware("http") 给响应添加了自定义'
                "响应头 X-Process-Time，浏览器跨域请求（CORS）时读不到它，为什么？"
            ),
        },
        CONFIG,
    )

    assert "__interrupt__" not in result
    # 中间件/跨域/响应头描述可识别为请求处理阶段，无需再问。
    assert result["fault_info"].component == "http_api"
    assert result["clarification_questions"] == []
    assert result["status"] == RunStatus.COMPLETED
    # 无日志时描述关键词参与检索词，保证仅凭描述也能召回证据。
    assert any("cors" in query for query in result["plan"].search_queries)


def test_clarify_skip_proceeds_without_reasking(make_fake_retriever, fake_llm) -> None:
    """澄清回合全部留空（跳过）时不再重复追问，凭现有信息继续诊断。"""
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}), llm=fake_llm
    )
    graph.invoke({**INPUT, "description": "接口出错了"}, CONFIG)
    # 注意不能传空 dict：LangGraph 把它当作"没有恢复值"，interrupt 原样
    # 再抛；跳过必须用非空哨兵占位（API 层同样如此）。
    resumed = graph.invoke(Command(resume=CLARIFY_SKIPPED_RESUME), CONFIG)

    assert resumed["status"] == RunStatus.COMPLETED
    assert resumed["clarify_rounds"] == 1
    assert resumed["clarify_declined"] == ["component", "logs"]


def test_traceback_feeds_queries_and_grade(make_fake_retriever, fake_llm) -> None:
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}), llm=fake_llm
    )
    result = graph.invoke({**INPUT, "logs": TRACEBACK_LOGS}, CONFIG)

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
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}), llm=fake_llm
    )
    result = graph.invoke(
        {
            **INPUT,
            "description": "容器启动正常但宿主机 curl 连接被拒",
            "logs": "INFO:     Uvicorn running on http://0.0.0.0:8000\n"
            "curl: (7) Failed to connect to localhost port 8000: Connection refused",
        },
        CONFIG,
    )

    # 无异常栈：宽泛词 + 日志尾行关键词精确词。
    queries = result["plan"].search_queries
    assert len(queries) == 2
    assert "connection" in queries[1] and "refused" in queries[1]


def test_grade_loop_rewrites_up_to_cap(make_fake_retriever) -> None:
    llm = FakeLLM(
        grade=EvidenceGrade(
            sufficient=False,
            reason="证据未覆盖连接池耗尽的关键信息",
            missing_terms=["operationalerror", "connection"],
        )
    )
    graph = build(
        retriever=make_fake_retriever(
            {"docs/quickstart.md": "# Quickstart\n\nFastAPI basics. " * 20}
        ),
        llm=llm,
    )
    result = graph.invoke({**INPUT, "logs": TRACEBACK_LOGS}, CONFIG)

    # LLM 评分不足：重写词用评分缺失项，重写两轮后到达上限。
    assert result["grade"].sufficient is False
    assert result["retry_count"] == 2
    # 重写词 = 组件 + 评分缺失项的前两个。
    assert result["plan"].search_queries == ["database operationalerror connection"]
    # 证据不足也不阻塞诊断，但结论必须经过审查。
    assert result["status"] == RunStatus.COMPLETED
    assert result["review"].passed is False


def test_grade_llm_failure_falls_back_to_rules(make_fake_retriever) -> None:
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}),
        llm=FakeLLM(grade_error=RuntimeError("grader unavailable")),
    )
    result = graph.invoke({**INPUT, "logs": TRACEBACK_LOGS}, CONFIG)

    # LLM 评分失败回退规则版：证据含异常关键字 → 足够，不重写。
    assert result["grade"].sufficient is True
    assert result.get("retry_count", 0) == 0
    assert result["status"] == RunStatus.COMPLETED


class ScriptedRetriever:
    """按查询关键词返回不同证据的假检索器：验证改写轮不淘汰已有证据。"""

    def search(self, query, source_type, k=None):
        if "round1" in query:
            return [
                Evidence(
                    doc_id="case-db",
                    parent_id="case-db_p0",
                    content="数据库连接失败案例",
                    score=0.9,
                    source_type="incident_case",
                )
            ]
        return [
            Evidence(
                doc_id="cors-tutorial",
                parent_id="cors-tutorial_p1",
                content="CORS 配置教程",
                score=0.6,
                source_type="official_doc",
            )
        ]


def test_retrieve_accumulates_evidence_across_rewrites() -> None:
    """改写只替换检索词：首轮高相关证据必须保留进后续证据池。"""
    retrieve = make_retrieve_node(ScriptedRetriever())
    plan = InvestigationPlan(
        hypotheses=[], search_queries=["round1 database"], verification_steps=[]
    )
    first = retrieve({"plan": plan})
    assert [e.parent_id for e in first["evidence"]] == ["case-db_p0"]

    rewritten = plan.model_copy(update={"search_queries": ["round2 cors"]})
    second = retrieve({"plan": rewritten, "evidence": first["evidence"]})
    ids = [e.parent_id for e in second["evidence"]]
    # 按分数降序：首轮 0.9 分的证据仍在且排前，新轮证据并入其后。
    assert ids == ["case-db_p0", "cors-tutorial_p1"]


def test_review_flags_unknown_citation(make_fake_retriever) -> None:
    bad = DiagnosisReport(
        most_likely_cause="连接池配置错误",
        confidence=0.9,
        supporting_evidence=["不存在的证据_p0"],
        fix_suggestions=["调大 pool_size"],
    )
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}),
        llm=FakeLLM(reports=[bad, bad]),
    )
    result = graph.invoke(
        {**INPUT, "logs": "sqlalchemy OperationalError connection refused"}, CONFIG
    )

    # 重试后仍引用无效 id：review 如实标注（重试不是洗白）。
    assert result["review"].passed is False
    assert any("不存在" in issue for issue in result["review"].issues)
    assert result["status"] == RunStatus.COMPLETED


def test_diagnose_retries_once_on_invalid_citation(make_fake_retriever) -> None:
    bad = DiagnosisReport(
        most_likely_cause="连接池配置错误",
        confidence=0.9,
        supporting_evidence=["不存在的证据_p0"],
        fix_suggestions=["调大 pool_size"],
    )
    good = DiagnosisReport(
        most_likely_cause="容器内 localhost 指向容器自身",
        confidence=0.85,
        supporting_evidence=["case-db_p0"],
        fix_suggestions=["把 localhost 改为 compose 服务名"],
        citations=["case-db"],
    )
    llm = FakeLLM(reports=[bad, good])
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}), llm=llm
    )
    result = graph.invoke(
        {**INPUT, "logs": TRACEBACK_LOGS}, CONFIG
    )

    # 重试一次后引用合法：review 通过，诊断采用第二次输出。
    # prompts = [评分, 首次诊断, 重试诊断]。
    assert len(llm.prompts) == 3
    assert "未检索到的证据 id" in llm.prompts[-1]
    assert result["diagnosis"].supporting_evidence == ["case-db_p0"]
    assert result["review"].passed is True
    assert result["status"] == RunStatus.COMPLETED


def test_dangerous_suggestion_pauses_for_confirmation(make_fake_retriever) -> None:
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}),
        llm=FakeLLM(
            report=DiagnosisReport(
                most_likely_cause="数据卷损坏",
                confidence=0.7,
                supporting_evidence=["case-db_p0"],
                fix_suggestions=["执行 docker volume rm db_data 后重建"],
            )
        ),
    )
    paused = graph.invoke(
        {**INPUT, "logs": "sqlalchemy OperationalError connection refused"}, CONFIG
    )

    assert "__interrupt__" in paused
    value = paused["__interrupt__"][0].value
    assert value["type"] == "confirmation"
    assert value["dangerous_commands"] == ["执行 docker volume rm db_data 后重建"]


def test_confirmation_approve_completes_reject_fails(make_fake_retriever) -> None:
    llm = FakeLLM(
        report=DiagnosisReport(
            most_likely_cause="数据卷损坏",
            confidence=0.7,
            supporting_evidence=["case-db_p0"],
            fix_suggestions=["执行 docker volume rm db_data 后重建"],
        )
    )
    graph = build(retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}), llm=llm)
    logs = "sqlalchemy OperationalError connection refused"

    approve_config = {"configurable": {"thread_id": "approve"}}
    graph.invoke({**INPUT, "logs": logs}, approve_config)
    approved = graph.invoke(Command(resume={"approved": True}), approve_config)
    assert approved["status"] == RunStatus.COMPLETED
    assert approved["review"].confirmed is True
    assert approved["review"].needs_confirmation is True

    reject_config = {"configurable": {"thread_id": "reject"}}
    graph.invoke({**INPUT, "logs": logs}, reject_config)
    rejected = graph.invoke(Command(resume={"approved": False}), reject_config)
    assert rejected["status"] == RunStatus.FAILED
    assert "拒绝" in (rejected.get("error") or "")


def test_events_flow_through_injected_sink(make_fake_retriever, fake_llm) -> None:
    events: list[dict] = []
    graph = build(
        retriever=make_fake_retriever({"cases/case-db.md": CASE_MD}), llm=fake_llm
    )
    config = {
        "configurable": {
            "thread_id": "sink-test",
            "event_sink": lambda event: events.append(event),
        }
    }
    graph.invoke({**INPUT, "logs": TRACEBACK_LOGS}, config)

    types = [event["type"] for event in events]
    assert "node_started" in types
    assert "input_analyzed" in types
    assert "tool_called" in types
    assert "evidence_found" in types
    assert "evidence_graded" in types
    assert "diagnosis_generated" in types
    assert "review_completed" in types
