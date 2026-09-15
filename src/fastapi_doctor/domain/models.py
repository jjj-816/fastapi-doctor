"""故障诊断领域的数据模型。

API、LangGraph 节点和后续持久化层共享这些模型，从而保证模型输出和接口响应
具有稳定结构，而不是在模块之间传递难以校验的任意字典。
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    """一次诊断运行在当前最小工作流中的状态。"""

    RUNNING = "running"
    NEEDS_CLARIFICATION = "needs_clarification"
    PLANNED = "planned"
    RETRIEVED = "retrieved"
    DIAGNOSED = "diagnosed"
    NEEDS_CONFIRMATION = "needs_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"


class SourceType(StrEnum):
    """知识库三种来源类型，与导入时的 metadata.source_type 一致。"""

    OFFICIAL_DOC = "official_doc"
    INCIDENT_CASE = "incident_case"
    RUNBOOK = "runbook"


class FaultInfo(BaseModel):
    """从用户描述、日志、代码和配置中提取的结构化故障信息。"""

    framework: str | None = None
    component: str | None = None
    exception_type: str | None = None
    http_status: int | None = None
    symptoms: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class InvestigationPlan(BaseModel):
    """检索前的调查计划；这里只记录假设和验证方向，不代表最终结论。"""

    hypotheses: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    """一条检索证据：子块命中后扩展出的父块级上下文（§5.2）。"""

    doc_id: str
    parent_id: str
    content: str
    title: str = ""
    section: str = ""
    source_type: str = ""
    components: list[str] = Field(default_factory=list)
    error_types: list[str] = Field(default_factory=list)
    risk_level: str = ""
    applies_to: str = ""
    source_url: str = ""

    @classmethod
    def from_parent(cls, payload: dict) -> "Evidence":
        """从 ParentStore.load_content 的返回构造证据。"""
        metadata = payload.get("metadata", {})
        headers = [
            str(metadata[key])
            for key in ("H1", "H2", "H3")
            if metadata.get(key)
        ]
        return cls(
            doc_id=str(metadata.get("source", "")),
            parent_id=payload.get("parent_id", ""),
            content=payload.get("content", ""),
            title=str(metadata.get("title", "")),
            section=" -> ".join(headers),
            source_type=str(metadata.get("source_type", "")),
            components=[str(item) for item in metadata.get("components", [])],
            error_types=[str(item) for item in metadata.get("error_types", [])],
            risk_level=str(metadata.get("risk_level", "")),
            applies_to=str(metadata.get("applies_to", "")),
            source_url=str(metadata.get("source_url", "")),
        )


class TracebackInfo(BaseModel):
    """analyze_traceback 提取的结构化异常信息（§4.4）。"""

    exception_chain: list[str] = Field(default_factory=list)
    root_exception: str | None = None
    root_message: str = ""
    files: list[str] = Field(default_factory=list)
    line_numbers: list[int] = Field(default_factory=list)
    key_error_strings: list[str] = Field(default_factory=list)


class EvidenceGrade(BaseModel):
    """对当前证据集合的确定性评分结果（§4.5 的 MVP 规则版）。"""

    sufficient: bool
    reason: str = ""
    missing_terms: list[str] = Field(default_factory=list)


class DiagnosisReport(BaseModel):
    """诊断节点输出（设计 §4.6），只允许基于引用证据下结论。"""

    most_likely_cause: str
    confidence: float = Field(ge=0, le=1)
    supporting_evidence: list[str] = Field(default_factory=list)
    investigation_steps: list[str] = Field(default_factory=list)
    fix_suggestions: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    alternative_causes: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class ReviewResult(BaseModel):
    """审查节点输出：证据一致性为确定性检查，危险命令交给人工确认。"""

    passed: bool
    issues: list[str] = Field(default_factory=list)
    dangerous_commands: list[str] = Field(default_factory=list)
    needs_confirmation: bool = False


class DiagnosisRequest(BaseModel):
    """用户提交的原始故障材料，并在 API 边界限制各字段大小。"""

    description: str = Field(min_length=1, max_length=10_000)
    logs: str = Field(default="", max_length=50_000)
    code: str = Field(default="", max_length=50_000)
    config: str = Field(default="", max_length=20_000)


class DiagnosisResponse(BaseModel):
    """当前同步演示接口返回的诊断图状态摘要。"""

    run_id: str
    status: RunStatus
    fault_info: FaultInfo
    clarification_questions: list[str]
    plan: InvestigationPlan | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    grade: EvidenceGrade | None = None
    diagnosis: DiagnosisReport | None = None
    review: ReviewResult | None = None
