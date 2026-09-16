"""FastAPI HTTP 入口：异步任务 + SSE 事件 + 恢复 + 反馈。

任务在进程内 asyncio 任务里经工作线程执行图；事件
先落库再实时推送，SSE 断线用 ?after=<seq> 或 Last-Event-ID 续播。
进程重启不会恢复后台任务，README 需说明；生产环境应换持久化任务队列。
"""

import asyncio
import html
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
from fastapi_doctor.graph.nodes import CLARIFY_SKIPPED_RESUME
from fastapi_doctor.ingestion import IngestionError, KnowledgeImporter, OllamaUnavailableError
from fastapi_doctor.llm import build_llm
from fastapi_doctor.pdf_ingest import has_extractable_text, pdf_bytes_to_markdown
from fastapi_doctor.retrieval.parent_store import ParentStore
from fastapi_doctor.retrieval.retriever import KnowledgeRetriever
from fastapi_doctor.runs import STREAM_TERMINAL_EVENTS, RunManager, execute_run
from fastapi_doctor.security import mask_secrets


def _build_graph():
    """构建带 Checkpointer 的诊断图；SQLite 可用则持久化恢复点。

    检索器实例同时挂到 app.state：上传入库必须与运行中的图共享同一份
    本地 Qdrant 客户端，避免并发打开两份索引。
    """
    checkpointer = None
    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(config.CHECKPOINT_DB_PATH, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
    retriever = KnowledgeRetriever()
    app.state.retriever = retriever
    app.state.checkpointer = checkpointer
    return build_diagnosis_graph(
        retriever=retriever, llm=build_llm(), checkpointer=checkpointer
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建运行管理器、图与父块存储；测试可替换 _build_graph 工厂注入假实现。"""
    app.state.run_manager = RunManager(config.APPLICATION_DB_PATH)
    app.state.run_manager.attach_loop(asyncio.get_running_loop())
    app.state.run_manager.fail_stale_running()
    app.state.graph = _build_graph()
    # 显式传参：默认参数在导入时求值，运行时替换 config 路径（测试）不生效。
    app.state.parent_store = ParentStore(config.PARENT_STORE_PATH)
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


@app.delete("/api/runs/{run_id}", status_code=204)
def delete_run(run_id: str, req: Request) -> Response:
    """删除一次诊断运行（记录、事件与图检查点）；执行中的运行不允许删除。"""
    manager: RunManager = req.app.state.run_manager
    row = manager.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    if row["status"] == RunStatus.RUNNING:
        raise HTTPException(status_code=409, detail="运行进行中，暂不能删除")
    manager.delete_run(run_id)
    # thread_id 与 run_id 相同：一并清理图检查点，避免中断态残留。
    checkpointer = getattr(req.app.state, "checkpointer", None)
    if checkpointer is not None:
        checkpointer.delete_thread(run_id)
    return Response(status_code=204)


@app.post("/api/runs/{run_id}/resume", response_model=RunCreated)
async def resume_run(run_id: str, request: RunResumeRequest, req: Request) -> RunCreated:
    """恢复两类中断：澄清回合传 answers，危险确认回合传 approved。"""
    manager: RunManager = req.app.state.run_manager
    row = manager.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="运行不存在")

    status = row["status"]
    if status == RunStatus.WAITING_CLARIFICATION:
        # 空 answers 表示用户跳过澄清：LangGraph 把空 dict 的 resume 值当作
        # "没有恢复值"（interrupt 原样再抛），用哨兵占位。
        resume_value: dict = request.answers or CLARIFY_SKIPPED_RESUME
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
            history = manager.events_after(run_id, seq)
            # 历史里的暂停事件（澄清/确认）不一定是流的终点：运行恢复后会
            # 继续产生后续事件。只有当它是最后一条历史事件（运行确实停在
            # 该暂停点或已终态）时才提前收流，否则回放到真实终点——
            # 否则回放含历史中断的已完成运行时，前端状态会永远停在"等待"。
            last_seq = history[-1]["seq"] if history else None
            for event in history:
                seq = event["seq"]
                yield _sse(event)
                if (
                    event["type"] in STREAM_TERMINAL_EVENTS
                    and event["seq"] == last_seq
                ):
                    return
            while True:
                if await req.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 心跳：防止代理断开空闲连接。
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
    """提交根因与解决方案反馈，用于后续评测与知识库补充。"""
    manager: RunManager = req.app.state.run_manager
    if manager.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    manager.add_feedback(run_id, request.rating, request.root_cause, request.solution)
    return {"ok": True}


_KB_PAGE_CSS = (
    "body{font-family:system-ui,-apple-system,sans-serif;max-width:860px;"
    "margin:32px auto;padding:0 16px;line-height:1.7;color:#24292f}"
    "h1{font-size:20px;margin-bottom:4px}"
    ".meta{color:#6b7280;font-size:13px;margin-top:0}"
    "pre{white-space:pre-wrap;word-break:break-word;background:#f6f7f9;"
    "border:1px solid #e5e7eb;border-radius:8px;padding:16px;font-size:14px}"
)


def _kb_html(title: str, meta: str, content: str) -> HTMLResponse:
    """渲染本地知识库原文页：内容全部来自本地存储，离线部署同样可用。"""
    safe_title = html.escape(title)
    safe_meta = html.escape(meta)
    page = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{safe_title} · FastAPI Doctor 知识库</title>"
        f"<style>{_KB_PAGE_CSS}</style></head><body>"
        f"<h1>{safe_title}</h1><p class='meta'>{safe_meta}</p>"
        f"<pre>{html.escape(content)}</pre></body></html>"
    )
    return HTMLResponse(page)


@app.get("/api/kb/docs")
def list_kb_documents(req: Request) -> list[dict]:
    """枚举本地知识库全部来源文档，供前端知识库浏览页使用。

    必须先于 /api/kb/{parent_id} 声明，否则 "docs" 会被当作 parent_id。
    """
    store: ParentStore = req.app.state.parent_store
    return store.list_documents()


MAX_UPLOAD_BYTES = 2 * 1024 * 1024


@app.post("/api/kb/upload")
async def upload_kb_documents(
    req: Request, files: list[UploadFile] = File(...)
) -> dict:
    """上传文档入库：Markdown 直接落盘，PDF 先转 Markdown，再复用既有导入流程。

    与运行中的诊断图共享同一检索器（同一份本地 Qdrant 客户端）；
    同名/非法文件在落盘前拒绝，导入结果按 imported/skipped/failed 如实回报。
    """
    retriever: KnowledgeRetriever = req.app.state.retriever
    markdown_dir = config.MARKDOWN_DIR
    existing = {path.stem.lower() for path in markdown_dir.rglob("*.md")}
    saved: list[str] = []
    rejected: list[dict] = []
    batch_stems: set[str] = set()
    for upload in files:
        name = Path(upload.filename or "").name
        stem = Path(name).stem.lower()
        suffix = name.lower().rsplit(".", 1)[-1] if "." in name else ""
        if suffix not in ("md", "markdown", "pdf"):
            rejected.append(
                {
                    "filename": upload.filename or name,
                    "reason": "仅支持 .md / .markdown / .pdf",
                }
            )
        elif stem in existing or stem in batch_stems:
            rejected.append({"filename": name, "reason": "知识库已存在同名文档"})
        else:
            content = await upload.read()
            if len(content) > MAX_UPLOAD_BYTES:
                rejected.append({"filename": name, "reason": "单文件超过 2MB"})
                continue
            if suffix == "pdf":
                try:
                    text = pdf_bytes_to_markdown(content)
                except Exception as exc:
                    rejected.append(
                        {"filename": name, "reason": f"PDF 解析失败：{type(exc).__name__}"}
                    )
                    continue
                # 页分隔符之外没有正文，视为无文本层的扫描件。
                if not has_extractable_text(text):
                    rejected.append(
                        {"filename": name, "reason": "PDF 未提取到文本（可能是扫描件）"}
                    )
                    continue
                (markdown_dir / f"{Path(name).stem}.md").write_text(text, encoding="utf-8")
            else:
                (markdown_dir / name).write_bytes(content)
            saved.append(name)
            batch_stems.add(stem)
    empty: dict = {
        "saved": [],
        "rejected": rejected,
        "imported": [],
        "skipped": [],
        "failed": [],
    }
    if not saved:
        return empty

    importer = KnowledgeImporter(
        markdown_dir=markdown_dir,
        parent_store=retriever.parent_store,
        vector_manager=retriever.vector_manager,
    )
    try:
        report = await asyncio.to_thread(importer.import_all)
    except OllamaUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "saved": saved,
        "rejected": rejected,
        "imported": [r.path.name for r in report.results if r.status == "imported"],
        "skipped": [r.path.name for r in report.results if r.status == "skipped"],
        "failed": [
            {"file": r.path.name, "error": r.error}
            for r in report.results
            if r.status == "failed"
        ],
    }


@app.get("/api/kb/doc/{doc_id}")
def kb_document(doc_id: str, req: Request) -> Response:
    """本地知识库整篇文档视图：参考资料引用的原文查看入口。

    浏览器导航（Accept: text/html）返回 HTML 页；带 Accept: application/json
    的程序化请求返回 JSON，供前端应用内阅读。
    """
    store: ParentStore = req.app.state.parent_store
    try:
        doc = store.load_document(doc_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="知识库中不存在该文档")
    metadata = doc["metadata"]
    title = doc["title"]
    parts = [doc["doc_id"], f"{doc['block_count']} 个父块"]
    if metadata.get("source_url"):
        parts.append(f"原始来源 {metadata['source_url']}")  # 纯文本展示，不提供跳转
    payload = {
        "doc_id": doc["doc_id"],
        "title": title,
        "source_type": str(metadata.get("source_type", "")),
        "source_url": str(metadata.get("source_url", "")),
        "block_count": doc["block_count"],
        "content": doc["content"],
    }
    if "application/json" in req.headers.get("accept", ""):
        return JSONResponse(payload)
    return _kb_html(title, " · ".join(parts), doc["content"])


@app.get("/api/kb/{parent_id}")
def kb_parent(parent_id: str, req: Request) -> Response:
    """本地知识库父块视图：检索证据"查看原文"的落点。"""
    store: ParentStore = req.app.state.parent_store
    try:
        block = store.load_content(parent_id)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        raise HTTPException(status_code=404, detail="知识库中不存在该父块")
    metadata = block["metadata"]
    title = str(metadata.get("title") or block["parent_id"])
    headers = [str(metadata[key]) for key in ("H1", "H2", "H3") if metadata.get(key)]
    parts = [block["parent_id"], " -> ".join(headers)]
    if metadata.get("source_url"):
        parts.append(f"原始来源 {metadata['source_url']}")  # 纯文本展示，不提供跳转
    payload = {
        "parent_id": block["parent_id"],
        "title": title,
        "section": " -> ".join(headers),
        "source_type": str(metadata.get("source_type", "")),
        "source_url": str(metadata.get("source_url", "")),
        "content": block["content"],
    }
    if "application/json" in req.headers.get("accept", ""):
        return JSONResponse(payload)
    return _kb_html(title, " · ".join(part for part in parts if part), block["content"])


# 构建后的前端（frontend/dist）存在时由本服务托管，演示时无需单独起前端。
_dist_dir = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist_dir.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_dist_dir, html=True), name="frontend")
