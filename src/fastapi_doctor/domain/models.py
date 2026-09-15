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
    FAILED = "failed"


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
