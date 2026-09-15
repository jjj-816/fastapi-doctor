"""FastAPI HTTP 入口。

当前仅提供健康检查和同步调用的最小诊断接口，用于验证 Graph 状态流。
正式 MVP 会扩展为创建任务、SSE 事件、恢复中断和反馈等异步接口。
"""

from uuid import uuid4

from fastapi import FastAPI

from fastapi_doctor.domain.models import (
    DiagnosisRequest,
    DiagnosisResponse,
    RunStatus,
)
from fastapi_doctor.graph.builder import build_diagnosis_graph
from fastapi_doctor.llm import build_llm
from fastapi_doctor.retrieval.retriever import KnowledgeRetriever

app = FastAPI(title="FastAPI Doctor", version="0.1.0")
graph = build_diagnosis_graph(retriever=KnowledgeRetriever(), llm=build_llm())


@app.get("/api/health")
def health() -> dict[str, str]:
    """供本地检查或部署探针确认进程能够正常响应。"""
    return {"status": "ok"}


@app.post("/api/diagnose", response_model=DiagnosisResponse)
def diagnose(request: DiagnosisRequest) -> DiagnosisResponse:
    """运行一次最小诊断图，并返回输入分析、调查计划与分源检索证据。"""
    result = graph.invoke(
        {
            "run_id": str(uuid4()),
            "description": request.description,
            "logs": request.logs,
            "code": request.code,
            "config": request.config,
            "status": RunStatus.RUNNING,
        }
    )
    return DiagnosisResponse.model_validate(result)
