"""最小诊断图的节点实现。

这些规则节点是开发第一阶段的确定性实现，用来先跑通状态变化和条件路由；
它们不是最终智能诊断逻辑。后续会逐步替换为经过 Pydantic 校验的模型输出。
"""

import re

from fastapi_doctor.domain.models import (
    Evidence,
    FaultInfo,
    InvestigationPlan,
    RunStatus,
    SourceType,
)
from fastapi_doctor.graph.state import DiagnosisState


def analyze_input(state: DiagnosisState) -> dict:
    """从原始输入中提取框架、组件、HTTP 状态码和缺失信息。"""
    text = "\n".join(
        (state["description"], state["logs"], state["code"], state["config"])
    ).lower()

    status_match = re.search(r"\b(4\d\d|5\d\d)\b", text)
    http_status = int(status_match.group(1)) if status_match else None
    framework = "fastapi" if "fastapi" in text else None

    component = None
    if any(token in text for token in ("postgres", "sqlalchemy", "database", "数据库")):
        component = "database"
    elif any(token in text for token in ("uvicorn", "启动", "startup")):
        component = "startup"
    elif any(token in text for token in ("async", "await", "异步")):
        component = "asyncio"
    elif http_status is not None:
        component = "http_api"

    missing_information = []
    if framework is None:
        missing_information.append("framework")
    if component is None:
        missing_information.append("component")
    if not state["logs"].strip():
        missing_information.append("logs")

    fault_info = FaultInfo(
        framework=framework,
        component=component,
        http_status=http_status,
        symptoms=[state["description"].strip()],
        missing_information=missing_information,
    )
    return {"fault_info": fault_info}


def clarify_if_needed(state: DiagnosisState) -> dict:
    """把会影响后续诊断的缺失字段转换为具体澄清问题。"""
    questions = {
        "framework": "这是哪个 Python Web 框架和版本？",
        "component": "故障发生在启动、请求处理、异步任务还是数据库访问阶段？",
        "logs": "请提供完整异常类型和 traceback 的最后 20 行。",
    }
    missing = state["fault_info"].missing_information
    clarification_questions = [questions[item] for item in missing]
    status = (
        RunStatus.NEEDS_CLARIFICATION if clarification_questions else RunStatus.RUNNING
    )
    return {
        "clarification_questions": clarification_questions,
        "status": status,
    }


def plan(state: DiagnosisState) -> dict:
    """根据结构化故障信息生成首版假设、检索词和验证步骤。"""
    fault = state["fault_info"]
    query_parts = [
        value
        for value in (
            fault.framework,
            fault.component,
            str(fault.http_status) if fault.http_status else None,
        )
        if value
    ]
    query = " ".join(query_parts)
    plan = InvestigationPlan(
        hypotheses=[f"检查 {fault.component or '应用'} 的输入、配置与运行环境"],
        search_queries=[query],
        verification_steps=["用最小复现请求确认故障是否稳定出现"],
    )
    return {"plan": plan, "status": RunStatus.PLANNED}


def route_after_clarification(state: DiagnosisState) -> str:
    """信息不足时停止等待补充，否则进入调查规划节点。"""
    if state["status"] == RunStatus.NEEDS_CLARIFICATION:
        return "end"
    return "plan"


# 每个查询在每个来源类型上召回的子块数；父块去重后证据更少。
PER_SOURCE_K = 3


def make_retrieve_node(retriever):
    """构建 retrieve 节点；检索器可注入，测试用假实现不依赖 Ollama。

    确定性 MVP：对计划中的每个检索词，在三种来源上各召回最多
    PER_SOURCE_K 条证据，按 parent_id 去重后合并写入状态。
    后续将由 LLM 规划的查询与证据评分替代。
    """

    def retrieve(state: DiagnosisState) -> dict:
        plan = state["plan"]
        queries = plan.search_queries or [""]
        merged: dict[str, Evidence] = {}
        for query in queries:
            for source_type in SourceType:
                for evidence in retriever.search(query, source_type, k=PER_SOURCE_K):
                    merged.setdefault(evidence.parent_id, evidence)
        return {
            "evidence": list(merged.values()),
            "status": RunStatus.RETRIEVED,
        }

    return retrieve
