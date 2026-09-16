"""组装并编译故障诊断 LangGraph。

当前图验证：节点更新状态、条件边决定去向、分源检索、证据
评分/查询重写回路、LLM 诊断与审查，以及两类人工
介入（澄清、危险确认）——均通过 LangGraph interrupt 暂停，
由异步 API 以 Command(resume=...) 恢复。运行事件经 config
注入的 event_sink 发往 SSE。
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from fastapi_doctor.graph.nodes import (
    analyze_input,
    clarify_if_needed,
    make_diagnose_node,
    make_grade_node,
    make_plan_node,
    make_retrieve_node,
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
    checkpointer: BaseCheckpointSaver | None = None,
):
    """构建可 invoke 的诊断图实例；检索器、LLM 与 Checkpointer 均可注入。

    流程：analyze_input -> clarify_if_needed ->（补充后回到 analyze_input；
    不足且达上限则结束）plan -> retrieve -> grade_evidence ->（不足且未达
    上限）rewrite_query -> retrieve ... -> diagnose -> review -> END。
    澄清与危险确认会 interrupt 暂停，等待 API 层恢复。
    """
    builder = StateGraph(DiagnosisState)
    builder.add_node("analyze_input", analyze_input)
    builder.add_node("clarify_if_needed", clarify_if_needed)
    builder.add_node("plan", make_plan_node(llm))
    builder.add_node("retrieve", make_retrieve_node(retriever or KnowledgeRetriever()))
    builder.add_node("grade_evidence", make_grade_node(llm))
    builder.add_node("rewrite_query", rewrite_query)
    builder.add_node("diagnose", make_diagnose_node(llm))
    builder.add_node("review", review)

    builder.add_edge(START, "analyze_input")
    builder.add_edge("analyze_input", "clarify_if_needed")
    builder.add_conditional_edges(
        "clarify_if_needed",
        route_after_clarification,
        {"analyze": "analyze_input", "plan": "plan", "end": END},
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
    return builder.compile(checkpointer=checkpointer or MemorySaver())
