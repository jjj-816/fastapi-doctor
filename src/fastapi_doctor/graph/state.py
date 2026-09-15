"""LangGraph 共享状态定义。

每个节点只读取所需字段并返回局部更新，由 LangGraph 合并为完整 DiagnosisState。
随着检索和审查节点接入，此处会继续增加证据、重试次数和最终诊断报告字段。
"""

from typing import NotRequired, TypedDict

from fastapi_doctor.domain.models import (
    DiagnosisReport,
    Evidence,
    EvidenceGrade,
    FaultInfo,
    InvestigationPlan,
    ReviewResult,
    RunStatus,
    TracebackInfo,
)


class DiagnosisState(TypedDict):
    """一次诊断从输入分析到最终报告全过程携带的状态。"""

    run_id: str
    description: str
    logs: str
    code: str
    config: str
    status: RunStatus
    fault_info: NotRequired[FaultInfo]
    clarification_questions: NotRequired[list[str]]
    traceback_info: NotRequired[TracebackInfo]
    plan: NotRequired[InvestigationPlan]
    evidence: NotRequired[list[Evidence]]
    grade: NotRequired[EvidenceGrade]
    retry_count: NotRequired[int]
    diagnosis: NotRequired[DiagnosisReport]
    review: NotRequired[ReviewResult]
    error: NotRequired[str]
