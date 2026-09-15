"""FastAPI HTTP 入口（设计 §6.1）：异步任务 + SSE 事件 + 恢复 + 反馈。

任务在进程内 asyncio 任务里经工作线程执行图（§6.1 的 MVP 形态）；事件
先落库再实时推送，SSE 断线用 ?after=<seq> 或 Last-Event-ID 续播（§7）。
进程重启不会恢复后台任务，README 需说明；生产环境应换持久化任务队列。
"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph.types import Command

from fastapi_doctor import config
from fastapi_doctor.domain.models import (
    DiagnosisRequest,
    DiagnosisResponse,
    FeedbackRequest,
    RunCreated,
    RunResumeRequest,
    RunSnapshot,
    RunStatus,
    RunSummary,
)
from fastapi_doctor.graph.builder import build_diagnosis_graph
from fastapi_doctor.llm import build_llm
from fastapi_doctor.retrieval.retriever import KnowledgeRetriever
from fastapi_doctor.runs import STREAM_TERMINAL_EVENTS, RunManager, execute_run
from fastapi_doctor.security import mask_secrets


def _build_graph():
    """构建带 Checkpointer 的诊断图；SQLite 可用则持久化恢复点。"""
    checkpointer = None
    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(config.CHECKPOINT_DB_PATH, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
    return build_diagnosis_graph(
        retriever=KnowledgeRetriever(), llm=build_llm(), checkpointer=checkpointer
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建运行管理器与图；测试可替换 _build_graph 工厂注入假实现。"""
    app.state.run_manager = RunManager(config.APPLICATION_DB_PATH)
    app.state.run_manager.attach_loop(asyncio.get_running_loop())
    app.state.graph = _build_graph()
    yield
    app.state.run_manager.close()


app = FastAPI(title="FastAPI Doctor", version="0.2.0", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict[str, str]:
    """供本地检查或部署探针确认进程能够正常响应。"""
    return {"status": "ok"}


@app.get("/api/runs", response_model=list[RunSummary])
def list_runs(req: Request, limit: int = 50) -> list[RunSummary]:
    """列出最近运行（前端侧栏历史），按创建时间倒序。"""
    manager: RunManager = req.app.state.run_manager
    limit = min(max(limit, 1), 100)
    return [RunSummary(**row) for row in manager.list_runs(limit)]


@app.post("/api/runs", response_model=RunCreated, status_code=201)
async def create_run(request: DiagnosisRequest, req: Request) -> RunCreated:
    """创建诊断任务：立即返回 run_id，事件经 GET events 的 SSE 推送。"""
    manager: RunManager = req.app.state.run_manager
    run_id = str(uuid4())
    manager.create_run(run_id, run_id, request.description)
    manager.emit(run_id, "run_started", {"description": mask_secrets(request.description)[:200]})
    asyncio.create_task(
        asyncio.to_thread(
            execute_run,
            req.app.state.graph,
            manager,
            run_id,
            run_id,
            {
                "run_id": run_id,
                "description": request.description,
                "logs": request.logs,
                "code": request.code,
                "config": request.config,
                "status": RunStatus.RUNNING,
            },
        )
    )
    return RunCreated(run_id=run_id, thread_id=run_id, status=RunStatus.RUNNING)


@app.get("/api/runs/{run_id}", response_model=RunSnapshot)
def get_run(run_id: str, req: Request) -> RunSnapshot:
    """获取运行状态；终态运行附带完整诊断结果。"""
    manager: RunManager = req.app.state.run_manager
    row = manager.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    result = None
    if row["result_json"]:
        result = DiagnosisResponse.model_validate_json(row["result_json"])
    return RunSnapshot(
        run_id=row["run_id"],
        thread_id=row["thread_id"],
        status=RunStatus(row["status"]),
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        result=result,
    )


@app.post("/api/runs/{run_id}/resume", response_model=RunCreated)
async def resume_run(run_id: str, request: RunResumeRequest, req: Request) -> RunCreated:
    """恢复两类中断：澄清回合传 answers，危险确认回合传 approved。"""
    manager: RunManager = req.app.state.run_manager
    row = manager.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="运行不存在")

    status = row["status"]
    if status == RunStatus.WAITING_CLARIFICATION:
        resume_value: dict = request.answers
    elif status == RunStatus.WAITING_CONFIRMATION:
        if request.approved is None:
            raise HTTPException(
                status_code=422, detail="危险确认回合必须提供 approved 字段"
            )
        resume_value = {"approved": request.approved}
    else:
        raise HTTPException(status_code=409, detail=f"运行当前状态 {status} 不可恢复")

    manager.set_status(run_id, RunStatus.RUNNING)
    manager.emit(run_id, "run_resumed", {"via": status})
    asyncio.create_task(
        asyncio.to_thread(
            execute_run,
            req.app.state.graph,
            manager,
            run_id,
            row["thread_id"],
            Command(resume=resume_value),
        )
    )
    return RunCreated(
        run_id=run_id, thread_id=row["thread_id"], status=RunStatus.RUNNING
    )


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, req: Request, after: int = 0) -> StreamingResponse:
    """SSE 事件流：先补发历史事件（after/Last-Event-ID），再实时推送。

    流在等待用户操作或到达终态的事件上结束；前端恢复任务后重新连接。
    """
    manager: RunManager = req.app.state.run_manager
    if manager.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    last_id = req.headers.get("last-event-id")
    if last_id and last_id.isdigit():
        after = max(after, int(last_id))

    queue = manager.register_queue(run_id)

    async def event_stream():
        try:
            seq = after
            for event in manager.events_after(run_id, seq):
                seq = event["seq"]
                yield _sse(event)
                if event["type"] in STREAM_TERMINAL_EVENTS:
                    return
            while True:
                if await req.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 心跳（§7）：防止代理断开空闲连接。
                    yield ": ping\n\n"
                    continue
                if event["seq"] <= seq:
                    continue
                seq = event["seq"]
                yield _sse(event)
                if event["type"] in STREAM_TERMINAL_EVENTS:
                    return
        finally:
            manager.unregister_queue(run_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: dict) -> str:
    """编码一条 SSE 消息；event id 供断线续播。"""
    return (
        f"id: {event['seq']}\n"
        f"event: {event['type']}\n"
        f"data: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
    )


@app.post("/api/runs/{run_id}/feedback")
def submit_feedback(run_id: str, request: FeedbackRequest, req: Request) -> dict:
    """提交根因与解决方案反馈（§6.1），用于后续评测与知识库补充。"""
    manager: RunManager = req.app.state.run_manager
    if manager.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    manager.add_feedback(run_id, request.rating, request.root_cause, request.solution)
    return {"ok": True}


# 构建后的前端（frontend/dist）存在时由本服务托管，演示时无需单独起前端。
_dist_dir = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist_dir.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_dist_dir, html=True), name="frontend")
