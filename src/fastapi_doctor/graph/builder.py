"""组装并编译故障诊断 LangGraph。

当前最小图验证：节点更新状态、条件边决定去向、信息不足时提前结束、
分源检索（§4.4）与证据评分/查询重写回路（§4.5）、LLM 诊断与审查（§4.6）。
后续将在这里接入 interrupt/resume 人工确认与 SSE 事件流。
"""

from langgraph.graph import END, START, StateGraph

from fastapi_doctor.graph.nodes import (
    analyze_input,
    clarify_if_needed,
    grade_evidence,
    make_diagnose_node,
    make_retrieve_node,
    plan,
    rewrite_query,
    review,
    route_after_clarification,
    route_after_grade,
)
from fastapi_doctor.graph.state import DiagnosisState
from fastapi_doctor.retrieval.retriever import KnowledgeRetriever


def build_diagnosis_graph(
    retriever: KnowledgeRetriever | None = None,
    llm=None,
):
    """构建可直接 invoke 的诊断图实例；检索器与 LLM 均可注入以便测试。

    流程：plan -> retrieve -> grade_evidence ->（不足且未达上限）
    rewrite_query -> retrieve ... -> diagnose -> review -> END。
    危险修复建议在 review 被标记为待人工确认并结束。
    """
    builder = StateGraph(DiagnosisState)
    builder.add_node("analyze_input", analyze_input)
    builder.add_node("clarify_if_needed", clarify_if_needed)
    builder.add_node("plan", plan)
    builder.add_node("retrieve", make_retrieve_node(retriever or KnowledgeRetriever()))
    builder.add_node("grade_evidence", grade_evidence)
    builder.add_node("rewrite_query", rewrite_query)
    builder.add_node("diagnose", make_diagnose_node(llm))
    builder.add_node("review", review)

    builder.add_edge(START, "analyze_input")
    builder.add_edge("analyze_input", "clarify_if_needed")
    builder.add_conditional_edges(
        "clarify_if_needed",
        route_after_clarification,
        {"plan": "plan", "end": END},
    )
    builder.add_edge("plan", "retrieve")
    builder.add_edge("retrieve", "grade_evidence")
    builder.add_conditional_edges(
        "grade_evidence",
        route_after_grade,
        {"rewrite": "rewrite_query", "done": "diagnose"},
    )
    builder.add_edge("rewrite_query", "retrieve")
    builder.add_edge("diagnose", "review")
    builder.add_edge("review", END)
    return builder.compile()
