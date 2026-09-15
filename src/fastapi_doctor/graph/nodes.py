"""最小诊断图的节点实现。

这些规则节点是开发第一阶段的确定性实现，用来先跑通状态变化和条件路由；
它们不是最终智能诊断逻辑。后续会逐步替换为经过 Pydantic 校验的模型输出。
"""

import re

from fastapi_doctor import config
from fastapi_doctor.analysis import analyze_traceback
from fastapi_doctor.domain.models import (
    Evidence,
    EvidenceGrade,
    FaultInfo,
    InvestigationPlan,
    RunStatus,
    SourceType,
    TracebackInfo,
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

    traceback_info = analyze_traceback(state["logs"])
    fault_info = FaultInfo(
        framework=framework,
        component=component,
        exception_type=(
            traceback_info.root_exception.rsplit(".", 1)[-1]
            if traceback_info.root_exception
            else None
        ),
        http_status=http_status,
        symptoms=[state["description"].strip()],
        missing_information=missing_information,
    )
    return {"fault_info": fault_info, "traceback_info": traceback_info}


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
    """根据结构化故障信息生成首版假设、检索词和验证步骤。

    检索词最多两条：宽泛词（框架 + 组件 + 状态码）负责召回背景知识；
    异常栈存在时追加精确词（异常类名 + 根异常消息），命中 BM25 强项。
    """
    fault = state["fault_info"]
    traceback_info = state.get("traceback_info") or TracebackInfo()

    broad_terms = [
        value
        for value in (
            fault.framework,
            fault.component,
            str(fault.http_status) if fault.http_status else None,
        )
        if value
    ]
    queries = [" ".join(broad_terms)] if broad_terms else []

    if traceback_info.root_exception:
        class_name = traceback_info.root_exception.rsplit(".", 1)[-1]
        specific_terms = [fault.component or "", class_name, traceback_info.root_message[:120]]
        specific = " ".join(term for term in specific_terms if term)
        if specific and specific not in queries:
            queries.append(specific)

    plan = InvestigationPlan(
        hypotheses=[f"检查 {fault.component or '应用'} 的输入、配置与运行环境"],
        search_queries=queries or [""],
        verification_steps=["用最小复现请求确认故障是否稳定出现"],
    )
    return {"plan": plan, "status": RunStatus.PLANNED}


# 每个查询在每个来源类型上召回的子块数；父块去重后证据更少。
PER_SOURCE_K = 3


def route_after_clarification(state: DiagnosisState) -> str:
    """信息不足时停止等待补充，否则进入调查规划节点。"""
    if state["status"] == RunStatus.NEEDS_CLARIFICATION:
        return "end"
    return "plan"


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


def _grade_needles(traceback_info: TracebackInfo) -> list[str]:
    """从异常信息提取用于词面校验的关键词（MVP 规则版，中文消息暂不参与）。"""
    if not traceback_info.root_exception:
        return []
    class_name = traceback_info.root_exception.rsplit(".", 1)[-1]
    tokens = re.findall(r"[A-Za-z_][\w-]{4,}", traceback_info.root_message)
    return list(dict.fromkeys([class_name.lower(), *(t.lower() for t in tokens)]))


def grade_evidence(state: DiagnosisState) -> dict:
    """确定性证据评分：证据非空，且包含异常关键字时视为足够。

    日志中没有可校验的异常关键字时默认采信检索结果；本节点是 §4.5
    LLM Evidence Grader 到位前的规则版替身。
    """
    evidence = state.get("evidence", [])
    traceback_info = state.get("traceback_info") or TracebackInfo()
    needles = _grade_needles(traceback_info)

    if not evidence:
        grade = EvidenceGrade(
            sufficient=False,
            reason="未检索到任何证据",
            missing_terms=needles[:3],
        )
    elif not needles:
        grade = EvidenceGrade(
            sufficient=True,
            reason="日志中无可校验的异常关键字，默认采信检索结果",
        )
    else:
        contents = [item.content.lower() for item in evidence]
        matched = [
            needle
            for needle in needles
            if any(needle in content for content in contents)
        ]
        if matched:
            grade = EvidenceGrade(
                sufficient=True,
                reason=f"证据包含关键错误信息：{matched[:3]}",
            )
        else:
            grade = EvidenceGrade(
                sufficient=False,
                reason="证据未包含异常关键字",
                missing_terms=needles[:3],
            )
    return {"grade": grade}


def rewrite_query(state: DiagnosisState) -> dict:
    """把证据中未命中的异常关键字并入新检索词后重试（上限见 config）。"""
    fault = state["fault_info"]
    traceback_info = state.get("traceback_info") or TracebackInfo()
    retry_count = state.get("retry_count", 0) + 1

    contents = "\n".join(item.content.lower() for item in state.get("evidence", []))
    missing = [term for term in _grade_needles(traceback_info) if term not in contents]
    if not missing:
        missing = [fault.component or fault.framework or "故障"]

    new_query = " ".join([fault.component or "", *missing[:2]]).strip()
    plan = state["plan"].model_copy(update={"search_queries": [new_query]})
    return {"plan": plan, "retry_count": retry_count}


def route_after_grade(state: DiagnosisState) -> str:
    """证据足够、或重写次数达到上限时结束，否则进入查询重写。"""
    if state["grade"].sufficient:
        return "done"
    if state.get("retry_count", 0) >= config.MAX_QUERY_REWRITES:
        return "done"
    return "rewrite"
