"""最小诊断图的节点实现。

这些规则节点是开发第一阶段的确定性实现，用来先跑通状态变化和条件路由；
它们不是最终智能诊断逻辑。后续会逐步替换为经过 Pydantic 校验的模型输出。
"""

import re

from fastapi_doctor import config
from fastapi_doctor.analysis import analyze_traceback
from fastapi_doctor.domain.models import (
    DiagnosisReport,
    Evidence,
    EvidenceGrade,
    FaultInfo,
    InvestigationPlan,
    ReviewResult,
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
    # 本工具只诊断 FastAPI 服务，框架是前提而非待问字段；用户描述里
    # 提不到 "fastapi"（只说 Uvicorn、数据库等）不应触发澄清。
    framework = "fastapi"

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
    else:
        # 无异常栈时（curl 报错、ValidationError 片段等），日志最后一行通常
        # 是错误行；取其中的英文关键词追加精确词，补 BM25 的强项。
        queries = _append_log_tail_query(queries, state["logs"])

    plan = InvestigationPlan(
        hypotheses=[f"检查 {fault.component or '应用'} 的输入、配置与运行环境"],
        search_queries=queries or [""],
        verification_steps=["用最小复现请求确认故障是否稳定出现"],
    )
    return {"plan": plan, "status": RunStatus.PLANNED}


# 每个查询在每个来源类型上召回的子块数；父块去重后证据更少。
PER_SOURCE_K = 3

# 日志尾行关键词的噪声过滤；截取数量上限防止查询过长。
_LOG_STOPWORDS = {"the", "and", "with", "for", "from", "error", "info"}
_LOG_QUERY_MAX_TOKENS = 8


def _append_log_tail_query(queries: list[str], logs: str) -> list[str]:
    """从日志最后一行提取英文关键词，追加一条精确检索词。"""
    if not logs.strip():
        return queries
    last_line = logs.strip().splitlines()[-1].lower()
    tokens = [
        token
        for token in re.findall(r"[a-z0-9][\w.-]{2,}", last_line)
        if token not in _LOG_STOPWORDS
    ][:_LOG_QUERY_MAX_TOKENS]
    log_query = " ".join(tokens)
    if log_query and log_query not in queries:
        queries.append(log_query)
    return queries


def route_after_clarification(state: DiagnosisState) -> str:
    """信息不足时停止等待补充，否则进入调查规划节点。"""
    if state["status"] == RunStatus.NEEDS_CLARIFICATION:
        return "end"
    return "plan"


def make_retrieve_node(retriever):
    """构建 retrieve 节点；检索器可注入，测试用假实现不依赖 Ollama。

    确定性 MVP：对计划中的每个检索词，在三种来源上各召回最多
    PER_SOURCE_K 条证据，先按 parent_id 再按 doc 去重，按相关度分数
    降序合并写入状态。后续将由 LLM 规划的查询与证据评分替代。
    """

    def retrieve(state: DiagnosisState) -> dict:
        plan = state["plan"]
        queries = plan.search_queries or [""]
        merged: dict[str, Evidence] = {}
        for query in queries:
            for source_type in SourceType:
                for evidence in retriever.search(query, source_type, k=PER_SOURCE_K):
                    existing = merged.get(evidence.parent_id)
                    # 同一父块被多个查询/来源命中时保留更高分的那条。
                    if existing is None or evidence.score > existing.score:
                        merged[evidence.parent_id] = evidence
        # doc 级去重：同一文档只留分数最高的父块，避免一个文档占多个
        # 提示词预算位；随后按相关度降序，8 条预算由最相关证据优先占用。
        by_doc: dict[str, Evidence] = {}
        for evidence in merged.values():
            best = by_doc.get(evidence.doc_id)
            if best is None or evidence.score > best.score:
                by_doc[evidence.doc_id] = evidence
        evidence_list = sorted(by_doc.values(), key=lambda e: e.score, reverse=True)
        return {
            "evidence": evidence_list,
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


def build_diagnosis_prompt(state: DiagnosisState) -> str:
    """组装诊断提示词：故障材料 + 截断后的证据，要求只依据证据下结论。"""
    fault = state["fault_info"]
    evidence = state.get("evidence", [])[: config.MAX_EVIDENCE_ITEMS]
    evidence_blocks = []
    for item in evidence:
        content = item.content[: config.MAX_EVIDENCE_CHARS]
        evidence_blocks.append(
            f"[证据 {item.parent_id}] 来源={item.doc_id} 类型={item.source_type}"
            f" 标题={item.title or item.section}\n{content}"
        )
    evidence_text = "\n\n".join(evidence_blocks) or "（无检索证据）"

    return (
        "你是一名 Python Web 服务故障诊断专家。请只依据给定证据给出诊断，"
        "不要编造证据中不存在的事实；证据不足时降低置信度并说明。\n\n"
        f"## 故障描述\n{state['description']}\n\n"
        f"## 日志\n{state['logs'][:2000] or '（无）'}\n\n"
        f"## 结构化信息\n"
        f"- 框架: {fault.framework or '未知'}\n"
        f"- 组件: {fault.component or '未知'}\n"
        f"- 异常类型: {fault.exception_type or '未知'}\n"
        f"- HTTP 状态码: {fault.http_status or '未知'}\n\n"
        f"## 检索证据\n{evidence_text}\n\n"
        "请用中文输出 JSON，字段为：most_likely_cause（最可能原因）、"
        "confidence（0 到 1 的小数）、"
        "supporting_evidence（引用证据时只填证据块方括号里的 parent_id，"
        "例如 case-db_p0，禁止复制整行来源信息）、"
        "investigation_steps（排查步骤）、fix_suggestions（修复建议）、"
        "verification（验证方法）、alternative_causes（替代原因）、"
        "citations（参考资料列表，只填证据块的来源 URL 或 doc_id，禁止整段复制）。"
    )


def make_diagnose_node(llm):
    """构建 diagnose 节点；LLM 可注入，测试用假实现不依赖模型服务。"""

    def diagnose(state: DiagnosisState) -> dict:
        # GLM/DeepSeek 等 OpenAI 兼容服务对 json_schema 的服务端约束不严格，
        # 模型会用 ```json 围栏输出导致严格解析失败；这类服务改走 function
        # calling，从 tool_call 参数取结构化结果。本地 Ollama 用默认即可。
        if type(llm).__module__.startswith("langchain_openai"):
            structured = llm.with_structured_output(
                DiagnosisReport, method="function_calling"
            )
        else:
            structured = llm.with_structured_output(DiagnosisReport)
        report = structured.invoke(build_diagnosis_prompt(state))
        return {"diagnosis": report, "status": RunStatus.DIAGNOSED}

    return diagnose


def _find_dangerous_commands(suggestions: list[str]) -> list[str]:
    """扫描修复建议中的危险命令片段。"""
    dangerous = []
    for suggestion in suggestions:
        lowered = suggestion.lower()
        for pattern in config.DANGEROUS_COMMAND_PATTERNS:
            if pattern in lowered:
                dangerous.append(suggestion)
                break
    return dangerous


def review(state: DiagnosisState) -> dict:
    """确定性审查（§4.6）：引用一致性 + 危险命令人工确认。

    「证据问题返回检索节点」与 LangGraph interrupt 人工确认将在异步
    SSE 接口阶段接入；当前把危险建议标记为待确认并结束运行。
    """
    report = state["diagnosis"]
    evidence = state.get("evidence", [])
    issues: list[str] = []

    # 引用整篇已检索到的文档（doc_id）与引用具体父块（parent_id）都算合法。
    valid_ids = {item.parent_id for item in evidence} | {
        item.doc_id for item in evidence
    }
    cited = set(report.supporting_evidence)
    unknown = sorted(cited - valid_ids)
    if unknown:
        issues.append(f"结论引用了不存在或未检索到的证据：{unknown}")
    if evidence and not report.supporting_evidence:
        issues.append("已有检索证据，但结论未引用任何证据")

    dangerous = _find_dangerous_commands(report.fix_suggestions)

    return {
        "review": ReviewResult(
            passed=not issues,
            issues=issues,
            dangerous_commands=dangerous,
            needs_confirmation=bool(dangerous),
        ),
        "status": (
            RunStatus.NEEDS_CONFIRMATION
            if dangerous
            else RunStatus.COMPLETED
        ),
    }
