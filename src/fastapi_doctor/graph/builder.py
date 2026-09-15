"""组装并编译故障诊断 LangGraph。

当前最小图验证三个关键概念：节点更新状态、条件边决定去向、信息不足时提前结束。
后续将在这里接入 interrupt/resume、混合检索、证据评分、诊断和审查回路。
"""

from langgraph.graph import END, START, StateGraph

from fastapi_doctor.graph.nodes import (
    analyze_input,
    clarify_if_needed,
    plan,
    route_after_clarification,
)
from fastapi_doctor.graph.state import DiagnosisState


def build_diagnosis_graph():
    """构建可直接 invoke 的诊断图实例。"""
    builder = StateGraph(DiagnosisState)
    builder.add_node("analyze_input", analyze_input)
    builder.add_node("clarify_if_needed", clarify_if_needed)
    builder.add_node("plan", plan)

    builder.add_edge(START, "analyze_input")
    builder.add_edge("analyze_input", "clarify_if_needed")
    builder.add_conditional_edges(
        "clarify_if_needed",
        route_after_clarification,
        {"plan": "plan", "end": END},
    )
    builder.add_edge("plan", END)
    return builder.compile()
