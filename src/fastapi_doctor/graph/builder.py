"""组装并编译故障诊断 LangGraph。

当前最小图验证：节点更新状态、条件边决定去向、信息不足时提前结束，
以及分源检索（§4.4 三个 search 工具）接入 retrieve 节点。
后续将在这里接入 interrupt/resume、证据评分、查询重写、诊断和审查回路。
"""

from langgraph.graph import END, START, StateGraph

from fastapi_doctor.graph.nodes import (
    analyze_input,
    clarify_if_needed,
    grade_evidence,
    make_retrieve_node,
    plan,
    rewrite_query,
    route_after_clarification,
    route_after_grade,
)
from fastapi_doctor.graph.state import DiagnosisState
from fastapi_doctor.retrieval.retriever import KnowledgeRetriever


def build_diagnosis_graph(retriever: KnowledgeRetriever | None = None):
    """构建可直接 invoke 的诊断图实例；检索器可注入以便测试。

    流程：plan -> retrieve -> grade_evidence ->（不足且未达上限）
    rewrite_query -> retrieve ...；证据足够或重写两轮后结束。
    """
    builder = StateGraph(DiagnosisState)
    builder.add_node("analyze_input", analyze_input)
    builder.add_node("clarify_if_needed", clarify_if_needed)
    builder.add_node("plan", plan)
    builder.add_node("retrieve", make_retrieve_node(retriever or KnowledgeRetriever()))
    builder.add_node("grade_evidence", grade_evidence)
    builder.add_node("rewrite_query", rewrite_query)

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
        {"rewrite": "rewrite_query", "done": END},
    )
    builder.add_edge("rewrite_query", "retrieve")
    return builder.compile()
