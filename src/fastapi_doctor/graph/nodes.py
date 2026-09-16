"""诊断图的节点实现。

确定性规则节点（分析、澄清、重写、审查）与 LLM 节点（检索规划、证据
评分、诊断）并存：模型输出经 Pydantic 校验，LLM 未注入或调用失败时
回退规则实现，保证流水线不因单次模型调用中断。
"""

import re

from langgraph.config import get_config
from langgraph.types import interrupt

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
from fastapi_doctor.security import mask_secrets


def _emit(event_type: str, payload: dict) -> None:
    """把节点级事件交给注入的 event_sink（异步 API 的事件流）。

    sink 由运行器通过 config["configurable"]["event_sink"] 注入；
    单元测试或直接调用节点时没有 sink，静默跳过。
    """
    try:
        configurable = get_config().get("configurable") or {}
    except Exception:  # 图运行时之外没有 config 可取
        return
    sink = configurable.get("event_sink")
    if callable(sink):
        sink({"type": event_type, "payload": payload})


def _invoke_structured(llm, schema, prompt):
    """调 LLM 取经 Pydantic 校验的结构化输出。

    GLM/DeepSeek 等 OpenAI 兼容服务对 json_schema 的服务端约束不严格，
    模型会用 ```json 围栏输出导致严格解析失败；这类服务改走 function
    calling，从 tool_call 参数取结构化结果。本地 Ollama 用默认即可。
    """
    if type(llm).__module__.startswith("langchain_openai"):
        structured = llm.with_structured_output(schema, method="function_calling")
    else:
        structured = llm.with_structured_output(schema)
    return structured.invoke(prompt)


# 解释型提问的标志词：寻求原理或做法说明，而非报告一个可复现的故障现场。
# 这类问题通常没有 traceback 可粘贴，不应追问日志；"怎么办/怎么解决"也
# 算在内——没有日志时凭描述检索知识库作答，好过反复追问卡住用户。
_EXPLANATION_PATTERN = re.compile(
    r"为什么|什么原因|原因是什么|怎么回事|原理|如何|怎么|怎样"
    r"|什么区别|区别是什么|有何区别|正确做法|应该怎么|该怎么|能不能|可以吗|是否可以"
    r"|\bwhy\b|\bhow\b",
    re.IGNORECASE,
)


def _is_explanation_question(description: str) -> bool:
    """判断描述是否为解释型提问；这类问题不追问 traceback。"""
    return bool(_EXPLANATION_PATTERN.search(description))


def analyze_input(state: DiagnosisState) -> dict:
    """从原始输入中提取框架、组件、HTTP 状态码和缺失信息。

    四项输入先经过脱敏：脱敏后的文本贯穿后续提示词与持久化。
    """
    _emit("node_started", {"node": "analyze_input"})
    inputs = {key: mask_secrets(state[key]) for key in ("description", "logs", "code", "config")}
    text = "\n".join(inputs.values()).lower()

    status_match = re.search(r"\b(4\d\d|5\d\d)\b", text)
    http_status = int(status_match.group(1)) if status_match else None
    # 本工具只诊断 FastAPI 服务，框架是前提而非待问字段；用户描述里
    # 提不到 "fastapi"（只说 Uvicorn、数据库等）不应触发澄清。
    framework = "fastapi"

    component = state.get("component_answer") or None
    if component is None:
        if any(token in text for token in ("postgres", "sqlalchemy", "database", "数据库")):
            component = "database"
        elif any(token in text for token in ("uvicorn", "启动", "startup")):
            component = "startup"
        elif any(token in text for token in ("async", "await", "异步")):
            component = "asyncio"
        elif any(
            token in text
            for token in ("middleware", "中间件", "cors", "跨域", "响应头", "请求头")
        ):
            # 中间件、CORS、自定义请求/响应头等问题属于请求处理阶段。
            component = "http_api"
        elif http_status is not None:
            component = "http_api"

    missing_information = []
    if component is None:
        missing_information.append("component")
    # 解释型提问没有可粘贴的故障现场，不追问日志；用户在澄清回合明确
    # 跳过（留空）的字段也不再重复追问。
    if not inputs["logs"].strip() and not _is_explanation_question(
        inputs["description"]
    ):
        missing_information.append("logs")
    declined = set(state.get("clarify_declined", []))
    missing_information = [item for item in missing_information if item not in declined]

    traceback_info = analyze_traceback(inputs["logs"])
    fault_info = FaultInfo(
        framework=framework,
        component=component,
        exception_type=(
            traceback_info.root_exception.rsplit(".", 1)[-1]
            if traceback_info.root_exception
            else None
        ),
        http_status=http_status,
        symptoms=[inputs["description"].strip()],
        missing_information=missing_information,
    )
    _emit("input_analyzed", {"fault_info": fault_info.model_dump()})
    return {**inputs, "fault_info": fault_info, "traceback_info": traceback_info}


CLARIFY_QUESTIONS = {
    "component": "故障发生在启动、请求处理、异步任务还是数据库访问阶段？",
    "logs": "如有报错日志，请粘贴异常类型和 traceback 最后 20 行（没有可留空）。",
}

# 用户全部留空（跳过澄清）时的 resume 占位：LangGraph 把空 dict 的 resume
# 值当作"没有恢复值"，interrupt 会原样再抛，必须用非空 dict 占位；键名
# 不与字段名冲突，澄清节点将其视为所有字段都留空。
CLARIFY_SKIPPED_RESUME = {"_skipped": True}


def clarify_if_needed(state: DiagnosisState) -> dict:
    """信息不足时通过 interrupt 暂停等待用户补充（第一类人工介入）。

    resume 值是 {字段名: 补充文本} 的字典（component 也在此列）；留空的
    字段记入 clarify_declined 不再追问。全部留空时传 CLARIFY_SKIPPED_RESUME
    占位（空 dict 会被 LangGraph 当作无恢复值）。补充内容脱敏后并入输入，
    路由回 analyze_input 重新分析；补满、跳过或达到轮次上限
    （MAX_CLARIFY_ROUNDS）才放行/结束。
    """
    _emit("node_started", {"node": "clarify_if_needed"})
    missing = state["fault_info"].missing_information
    questions = [CLARIFY_QUESTIONS[item] for item in missing]
    rounds = state.get("clarify_rounds", 0)

    if not questions:
        # 补充信息重新分析后的第二轮经过这里：保留已问过的问题记录，
        # 不用空列表覆盖；首轮信息齐全时 state 里本就没有问题。
        return {
            "clarification_questions": state.get("clarification_questions", []),
            "clarify_rounds": rounds,
            "resumed": False,
            "status": RunStatus.RUNNING,
        }
    if rounds >= config.MAX_CLARIFY_ROUNDS:
        return {
            "clarification_questions": questions,
            "clarify_rounds": rounds,
            "resumed": False,
            "status": RunStatus.NEEDS_CLARIFICATION,
        }

    answers = interrupt(
        {
            "type": "clarification",
            "questions": questions,
            "missing": list(missing),
            "round": rounds + 1,
        }
    )
    # 留空的字段视为"用户没有该信息"：记录后后续轮次不再重复追问，
    # 而是凭现有信息继续诊断，避免反复追问卡住用户。
    declined = [item for item in missing if not (answers or {}).get(item)]
    updates: dict = {
        "clarification_questions": questions,
        "clarify_rounds": rounds + 1,
        "clarify_declined": [*state.get("clarify_declined", []), *declined],
        "resumed": True,
        "status": RunStatus.RUNNING,
    }
    for key in ("description", "logs", "code", "config"):
        value = (answers or {}).get(key)
        if value:
            updates[key] = mask_secrets(str(value))
    # "component" 不在四项输入里：用户回答的阶段单独入状态，
    # analyze_input 重新分析时优先采用（否则回答会被静默丢弃）。
    component_answer = (answers or {}).get("component")
    if component_answer:
        updates["component_answer"] = mask_secrets(str(component_answer))
    return updates


def _rule_based_plan(state: DiagnosisState) -> InvestigationPlan:
    """规则版检索计划：宽泛词召回背景知识，精确词命中 BM25 强项。

    检索词最多两条：宽泛词（框架 + 组件 + 状态码）；异常栈存在时追加
    精确词（异常类名 + 根异常消息），否则从日志尾行/描述提取关键词。
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
        if not state["logs"].strip():
            # 连日志都没有（解释型提问、用户跳过澄清）时，描述是唯一信息源。
            queries = _append_description_query(queries, state["description"])

    return InvestigationPlan(
        hypotheses=[f"检查 {fault.component or '应用'} 的输入、配置与运行环境"],
        search_queries=queries or [""],
        verification_steps=["用最小复现请求确认故障是否稳定出现"],
    )


def build_plan_prompt(state: DiagnosisState) -> str:
    """组装检索规划提示词（LLM 规划器）。"""
    fault = state["fault_info"]
    return (
        "你是一名 FastAPI 故障诊断助手的检索规划员。请依据故障信息制定检索"
        "计划，用于在本地知识库（FastAPI/Pydantic/SQLAlchemy/Uvicorn/Docker/"
        "OpenTelemetry/LangGraph 官方文档、历史故障案例与 Runbook）中召回证据。\n"
        "要求：\n"
        "1) search_queries 给 2~3 条检索词：第 1 条宽泛（框架与组件领域），"
        "其余精确（异常类名、状态码、配置项、报错原文中的关键词）；\n"
        "2) 检索词优先直接取自症状描述和日志的原文词（多为英文），不要发明"
        "日志里不存在的异常类名或配置项；\n"
        "3) hypotheses 给 1~3 条针对本次故障的可验证假设，具体、不要套话；\n"
        "4) verification_steps 给 1~2 条用户可执行的验证步骤。\n\n"
        "## 故障信息\n"
        f"- 框架: {fault.framework or '未知'}\n"
        f"- 组件: {fault.component or '未知'}\n"
        f"- 异常类型: {fault.exception_type or '未知'}\n"
        f"- HTTP 状态码: {fault.http_status or '未知'}\n"
        f"- 症状描述: {state['description'][:800] or '（无）'}\n"
        f"- 日志末尾: {state['logs'][-800:] or '（无）'}\n"
        f"- 相关代码: {state['code'][:400] or '（无）'}\n"
        f"- 相关配置: {state['config'][:400] or '（无）'}\n\n"
        "请用中文输出 JSON（检索词本身保持原文用词）。"
    )


def make_plan_node(llm):
    """构建 plan 节点：LLM 规划检索词与假设，未注入或失败时回退规则版。

    LLM 规划的优势是检索词贴合语料词汇（中文描述也能提出英文检索词）、
    假设针对本次故障而非套话；规则版保证无 LLM 时流水线照常运转。
    两者并用而非二选一：LLM 检索词在前，规则精确词（异常类名/日志尾行
    原文词）保底占最后一个名额——LLM 常把原文词改写成同义词或译成中文，
    而 BM25 命中案例库靠的恰恰是原文词（基线评测 Hit@5 的差别所在）。
    """

    def plan(state: DiagnosisState) -> dict:
        _emit("node_started", {"node": "plan"})
        llm_plan = None
        planner = "rule"
        if llm is not None:
            try:
                llm_plan = _invoke_structured(
                    llm, InvestigationPlan, build_plan_prompt(state)
                )
                planner = "llm"
            except Exception:  # 单次规划失败不阻塞诊断流水线
                llm_plan = None
        if llm_plan is None or not [q for q in llm_plan.search_queries if q.strip()]:
            llm_plan = _rule_based_plan(state)
            planner = "rule"
        queries = [q.strip() for q in llm_plan.search_queries if q.strip()]
        if planner == "llm":
            # 规则精确词保底并入末位名额（无精确词或已重复则跳过）。
            reserved = next(
                (q.strip() for q in _rule_based_plan(state).search_queries[1:] if q.strip()),
                None,
            )
            if reserved and reserved not in queries:
                queries = queries[: config.MAX_PLAN_QUERIES - 1]
                queries.append(reserved)
                planner = "llm+rule"
        # 防御：限制检索词数量，避免单个计划撑爆提示词预算；空假设回退模板。
        result_plan = llm_plan.model_copy(
            update={
                "search_queries": queries[: config.MAX_PLAN_QUERIES],
                "hypotheses": llm_plan.hypotheses
                or [
                    f"检查 {state['fault_info'].component or '应用'} 的输入、"
                    "配置与运行环境"
                ],
            }
        )
        _emit(
            "plan_created",
            {"search_queries": result_plan.search_queries, "planner": planner},
        )
        return {"plan": result_plan, "status": RunStatus.PLANNED}

    return plan


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


def _append_description_query(queries: list[str], description: str) -> list[str]:
    """无日志时从描述里提取英文/数字关键词，追加一条精确检索词。

    解释型提问（为什么/如何）没有日志可提，描述里的 middleware、CORS、
    异常类名等词是召回上传文档与案例的主要线索。
    """
    tokens = [
        token.lower()
        for token in re.findall(r"[A-Za-z0-9][\w.-]{2,}", description)
        if token.lower() not in _LOG_STOPWORDS
    ][:_LOG_QUERY_MAX_TOKENS]
    description_query = " ".join(tokens)
    if description_query and description_query not in queries:
        queries.append(description_query)
    return queries


def route_after_clarification(state: DiagnosisState) -> str:
    """补充过信息则回到分析节点重新提取，信息足够则规划，超限则结束。"""
    if state.get("resumed"):
        return "analyze"
    if state["status"] == RunStatus.NEEDS_CLARIFICATION:
        return "end"
    return "plan"


def make_retrieve_node(retriever):
    """构建 retrieve 节点；检索器可注入，测试用假实现不依赖 Ollama。

    确定性 MVP：对计划中的每个检索词，在三种来源上各召回最多
    PER_SOURCE_K 条证据，先按 parent_id 再按 doc 去重，按相关度分数
    降序合并写入状态。证据跨改写轮次累积：改写只替换检索词，不淘汰
    已有证据——否则首轮命中的高相关证据会被改写后变窄的检索词冲掉。
    """

    def retrieve(state: DiagnosisState) -> dict:
        _emit("node_started", {"node": "retrieve"})
        plan = state["plan"]
        queries = plan.search_queries or [""]
        _emit(
            "tool_called",
            {
                "tools": [
                    "search_official_docs",
                    "search_incident_cases",
                    "search_runbooks",
                ],
                "queries": queries,
            },
        )
        # 从上一轮证据出发合并（首轮为空）：同一父块保留更高分。
        merged: dict[str, Evidence] = {
            item.parent_id: item for item in state.get("evidence", [])
        }
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
        _emit(
            "evidence_found",
            {"doc_ids": [e.doc_id for e in evidence_list[: config.MAX_EVIDENCE_ITEMS]]},
        )
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


def _rule_based_grade(state: DiagnosisState) -> EvidenceGrade:
    """规则版证据评分：证据非空，且包含异常关键字时视为足够。

    日志中没有可校验的异常关键字时默认采信检索结果；作为 LLM 评分
    失败或未注入 LLM 时的回退实现。
    """
    evidence = state.get("evidence", [])
    traceback_info = state.get("traceback_info") or TracebackInfo()
    needles = _grade_needles(traceback_info)

    if not evidence:
        return EvidenceGrade(
            sufficient=False,
            reason="未检索到任何证据",
            missing_terms=needles[:3],
        )
    if not needles:
        return EvidenceGrade(
            sufficient=True,
            reason="日志中无可校验的异常关键字，默认采信检索结果",
        )
    contents = [item.content.lower() for item in evidence]
    matched = [
        needle
        for needle in needles
        if any(needle in content for content in contents)
    ]
    if matched:
        return EvidenceGrade(
            sufficient=True,
            reason=f"证据包含关键错误信息：{matched[:3]}",
        )
    return EvidenceGrade(
        sufficient=False,
        reason="证据未包含异常关键字",
        missing_terms=needles[:3],
    )


def build_grade_prompt(state: DiagnosisState) -> str:
    """组装证据评分提示词：相关性、来源可信度、环境匹配、假设支撑。"""
    fault = state["fault_info"]
    evidence = state.get("evidence", [])[: config.MAX_EVIDENCE_ITEMS]
    blocks = []
    for item in evidence:
        content = item.content[: config.MAX_EVIDENCE_CHARS]
        blocks.append(
            f"[证据 {item.parent_id}] doc_id={item.doc_id} 类型={item.source_type}"
            f" 标题={item.title or item.section}\n{content}"
        )
    evidence_text = "\n\n".join(blocks) or "（无检索证据）"

    return (
        "你是一名检索证据评审员。请依据以下四个维度判断证据是否足以支撑"
        "本次故障诊断：\n"
        "1) 相关性：证据内容是否与故障症状和异常直接相关；\n"
        "2) 来源可信度：故障案例与 Runbook 优先于一般教程；\n"
        "3) 环境匹配：证据描述的运行环境（容器、数据库、部署方式等）"
        "是否与故障环境一致；\n"
        "4) 假设支撑：证据是否覆盖诊断假设所需的关键错误信息。\n\n"
        f"## 故障信息\n"
        f"- 框架: {fault.framework or '未知'}\n"
        f"- 组件: {fault.component or '未知'}\n"
        f"- 异常类型: {fault.exception_type or '未知'}\n"
        f"- HTTP 状态码: {fault.http_status or '未知'}\n"
        f"- 症状: {state['description'][:500] or '（无）'}\n"
        f"- 日志末尾: {state['logs'][:500] or '（无）'}\n\n"
        f"## 诊断假设\n"
        + "\n".join(f"- {h}" for h in state["plan"].hypotheses)
        + f"\n\n## 检索证据\n{evidence_text}\n\n"
        "请用中文输出 JSON，字段为：sufficient（布尔值，证据是否足够）、"
        "reason（一两句判断理由）、missing_terms（字符串列表：证据缺失的"
        "关键信息，给具体的名词或关键词，禁止整句话；足够时给空列表）。"
    )


def _rule_decisive_grade(state: DiagnosisState) -> EvidenceGrade | None:
    """规则能明确裁决时给出评分，裁决不了返回 None 交 LLM 仲裁。

    明确不足：没有检索到任何证据（重写检索词即可，无需模型判断）。
    明确足够：日志的**异常类名**在证据词面命中——类名是强信号（问
    OperationalError 检索到讲 OperationalError 的文档，基本不会错）；
    消息词不算数：SQLAlchemy 池案例里的 reached/number 之类泛词命中
    会把 Redis 等跨技术故障误判成"足够"，绕过诚实降级。
    其余情况（无异常类名、类名未命中）规则难以区分"证据无关"与
    "表述不同"，交给 LLM。评分是每轮都跑的节点，规则裁掉一次就省
    一次 LLM 往返——与参考项目的耗时差距主要就在调用次数上。
    """
    evidence = state.get("evidence", [])
    if not evidence:
        return EvidenceGrade(
            sufficient=False,
            reason="未检索到任何证据",
            missing_terms=_grade_needles(
                state.get("traceback_info") or TracebackInfo()
            )[:3],
        )
    traceback_info = state.get("traceback_info") or TracebackInfo()
    if traceback_info.root_exception:
        class_name = traceback_info.root_exception.rsplit(".", 1)[-1].lower()
        contents = [item.content.lower() for item in evidence]
        if any(class_name in content for content in contents):
            return EvidenceGrade(
                sufficient=True,
                reason=f"证据包含异常类名 {class_name}（规则裁决）",
            )
    return None


def make_grade_node(llm):
    """构建证据评分节点（LLM Evidence Grader）。

    规则先行的两级评分：规则能明确裁决（空证据/关键词命中）直接出结果，
    裁决不了才调 LLM 仲裁；llm 为 None 或评分调用失败时回退完整规则版。
    评分给出的 missing_terms 会传给 rewrite_query 改写检索词。
    """

    def grade_evidence(state: DiagnosisState) -> dict:
        _emit("node_started", {"node": "grade_evidence"})
        grade = _rule_decisive_grade(state)
        if grade is None and llm is not None:
            try:
                grade = _invoke_structured(
                    llm, EvidenceGrade, build_grade_prompt(state)
                )
            except Exception:  # 单次评分失败不阻塞诊断流水线
                grade = None
        if grade is None:
            grade = _rule_based_grade(state)
        _emit("evidence_graded", grade.model_dump())
        return {"grade": grade}

    return grade_evidence


def rewrite_query(state: DiagnosisState) -> dict:
    """按评分缺失项改写检索词后重试（上限见 config）。"""
    _emit("node_started", {"node": "rewrite_query"})
    fault = state["fault_info"]
    traceback_info = state.get("traceback_info") or TracebackInfo()
    retry_count = state.get("retry_count", 0) + 1

    contents = "\n".join(item.content.lower() for item in state.get("evidence", []))
    # 优先用评分给出的缺失关键词（按失败原因改写）；评分器没给
    # 时退回规则推导的异常关键字。
    grade = state.get("grade")
    missing = [
        term
        for term in (grade.missing_terms if grade else [])
        if term and term.lower() not in contents
    ]
    if not missing:
        missing = [
            term for term in _grade_needles(traceback_info) if term not in contents
        ]
    if not missing:
        missing = [fault.component or fault.framework or "故障"]

    new_query = " ".join([fault.component or "", *missing[:2]]).strip()
    plan = state["plan"].model_copy(update={"search_queries": [new_query]})
    _emit("query_rewritten", {"query": new_query, "retry_count": retry_count})
    return {"plan": plan, "retry_count": retry_count}


def route_after_grade(state: DiagnosisState) -> str:
    """证据足够、或重写次数达到上限时结束，否则进入查询重写。"""
    if state["grade"].sufficient:
        return "done"
    if state.get("retry_count", 0) >= config.MAX_QUERY_REWRITES:
        return "done"
    return "rewrite"


def build_diagnosis_prompt(state: DiagnosisState) -> str:
    """组装诊断提示词：故障材料 + 截断后的证据，要求只依据证据下结论。

    评分节点判证据不足时（改写上限仍不足），把评分理由传给诊断——没有
    这段，诊断只看到表面相似的证据（如问 Redis 却检索到 SQLAlchemy 连接
    池案例），会理直气壮地类比推理并给出中高置信度（评测集外实测）。
    """
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
    parent_ids = [item.parent_id for item in evidence]
    doc_ids = sorted({item.doc_id for item in evidence})

    grade = state.get("grade")
    if grade is not None and not grade.sufficient:
        evidence_status = (
            "## 证据状态（评分节点判定：不足）\n"
            f"评分理由：{grade.reason}\n"
            "上述证据可能只与故障表面相似（如故障涉及 Redis 而证据讲"
            " SQLAlchemy 连接池）。因此：\n"
            "- 若证据与故障的技术/组件不匹配，most_likely_cause 必须如实"
            "说明\"本地知识库未覆盖该故障场景\"，confidence 不得超过 0.3，"
            "修复建议只给通用排查方向并注明证据缺口；\n"
            "- 严禁把不同技术的证据当作同类案例类比推理。\n\n"
        )
    else:
        evidence_status = ""

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
        f"{evidence_status}## 检索证据\n{evidence_text}\n\n"
        "请用中文输出 JSON，字段为：most_likely_cause（最可能原因）、"
        "confidence（0 到 1 的小数）、"
        "supporting_evidence（引用证据时只填证据块方括号里的 parent_id）、"
        "investigation_steps（排查步骤）、fix_suggestions（修复建议）、"
        "verification（验证方法）、alternative_causes（替代原因）、"
        "citations（参考资料列表，只填证据块的来源 URL 或 doc_id）。\n"
        "输出尽量精炼：investigation_steps、fix_suggestions、"
        "alternative_causes 各不超过 3 条，每条一两句话（输出越长等待越久）。\n"
        "supporting_evidence 只能从以下 parent_id 列表选取，citations 只能"
        "从以下 doc_id 列表选取，禁止编造列表之外的 id：\n"
        f"parent_id: {', '.join(parent_ids) or '（无）'}\n"
        f"doc_id: {', '.join(doc_ids) or '（无）'}"
    )


def make_diagnose_node(llm):
    """构建 diagnose 节点；LLM 可注入，测试用假实现不依赖模型服务。"""

    def diagnose(state: DiagnosisState) -> dict:
        _emit("node_started", {"node": "diagnose"})
        prompt = build_diagnosis_prompt(state)
        try:
            report = _invoke_structured(llm, DiagnosisReport, prompt)
        except Exception:
            # 瞬时失败（超时/限流）重试一次：plan/grade 失败都有规则回退，
            # 诊断是流程里唯一没有 Plan B 的 LLM 调用，至少别一击即溃；
            # 两次都失败才让运行失败（错误如实上抛）。
            _emit("diagnosis_retrying", {"reason": "llm_error"})
            report = _invoke_structured(llm, DiagnosisReport, prompt)

        # 引用自检：引用了未检索到的证据 id 时，把问题
        # 喂回模型重试一次；仍失败则交由 review 节点如实标注。citations
        # 允许填 URL，因此只自检 supporting_evidence。
        evidence = state.get("evidence", [])
        valid_ids = {item.parent_id for item in evidence} | {
            item.doc_id for item in evidence
        }
        unknown = sorted(set(report.supporting_evidence) - valid_ids)
        if unknown and llm is not None:
            _emit("diagnosis_retried", {"unknown_citations": unknown})
            retry_prompt = (
                f"你刚才的诊断引用了未检索到的证据 id：{unknown}。"
                "请只依据原始材料重新输出完整 JSON，所有 supporting_evidence"
                " 必须从合法 parent_id 列表中选取。\n\n" + prompt
            )
            try:
                report = _invoke_structured(llm, DiagnosisReport, retry_prompt)
            except Exception:  # 重试失败时保留首次结果，交给 review 标注
                pass

        _emit(
            "diagnosis_generated",
            {
                "most_likely_cause": report.most_likely_cause,
                "confidence": report.confidence,
            },
        )
        return {"diagnosis": report, "status": RunStatus.DIAGNOSED}

    return diagnose


# 否定语境词：危险片段前方（同一子句内）出现时，是"警告别做"而非建议
# 执行（如"不要用 docker volume rm 修复连接问题"），不应触发人工确认。
_NEGATION_PATTERN = re.compile(
    r"不要|切勿|避免|禁止|不应|不得|不能用|慎用|谨慎|拒绝|"
    r"do not|don't|never|avoid",
    re.IGNORECASE,
)


def _find_dangerous_commands(texts: list[str]) -> list[str]:
    """扫描文本中的危险命令片段（按子句匹配，带否定语境豁免）。

    以 。；\\n 切分出子句后逐个匹配：危险片段前方出现否定词的子句视为
    警告语境，不算危险——曾把模型"不要用 docker volume rm 修复"的提醒
    误当成危险建议（评测 E01）。
    """
    dangerous = []
    for text in texts:
        for clause in re.split(r"[。；\n]", text):
            lowered = clause.lower()
            hit = False
            for pattern in config.DANGEROUS_COMMAND_PATTERNS:
                for match in re.finditer(re.escape(pattern), lowered):
                    if not _NEGATION_PATTERN.search(lowered[: match.start()]):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                dangerous.append(text)
                break
    return dangerous


def review(state: DiagnosisState) -> dict:
    """确定性审查：引用一致性 + 危险命令 interrupt 人工确认。

    危险命令两个来源：修复建议（模型输出），以及用户描述本身提出的
    危险操作（如"运维同事建议删卷"——建议即使写成警告语境，场景仍需
    人工确认）。含危险内容时通过 interrupt 暂停（第二类人工介入），
    resume 值为 {"approved": true/false}；批准后正常完成，拒绝则运行
    失败结束。「证据问题返回检索节点」留待后续迭代。
    """
    _emit("node_started", {"node": "review"})
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

    # 确定性兜底：评分判证据不足（重写上限仍不足）时，无论模型结论写
    # 得多自信，审查都如实标注——不阻塞诊断，但用户能看到保留意见。
    grade = state.get("grade")
    if grade is not None and not grade.sufficient:
        issues.append("证据评分不足：结论基于弱相关证据，仅供参考")

    dangerous = _find_dangerous_commands(
        [*report.fix_suggestions, state["description"]]
    )

    if dangerous:
        decision = interrupt(
            {
                "type": "confirmation",
                "dangerous_commands": dangerous,
                "report": report.model_dump(),
            }
        )
        approved = bool(
            decision.get("approved") if isinstance(decision, dict) else decision
        )
        _emit(
            "review_completed",
            {"passed": not issues, "needs_confirmation": True, "approved": approved},
        )
        return {
            "review": ReviewResult(
                passed=not issues,
                issues=issues,
                dangerous_commands=dangerous,
                needs_confirmation=True,
                confirmed=approved,
            ),
            "status": RunStatus.COMPLETED if approved else RunStatus.FAILED,
            "error": None if approved else "用户拒绝了包含危险命令的修复建议",
        }

    _emit(
        "review_completed",
        {"passed": not issues, "needs_confirmation": False},
    )
    return {
        "review": ReviewResult(
            passed=not issues,
            issues=issues,
            dangerous_commands=dangerous,
            needs_confirmation=False,
        ),
        "status": RunStatus.COMPLETED,
    }
