"""运行管理（设计 §6.1/§6.2/§6.3）：任务记录、事件流与图执行器。

RunManager 用标准库 SQLite 落库运行、事件与反馈（data/application.db），
并把新事件推送给已注册的 asyncio.Queue 供 SSE 实时读取；图执行器
execute_run 在工作线程里 invoke 图，通过 config 注入的 event_sink 收
节点事件，处理两类 interrupt（澄清/危险确认）并把终态写回。
"""

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi_doctor.domain.models import RunStatus
from fastapi_doctor.security import mask_secrets

# SSE 流在这些事件上结束：等待用户操作或到达终态后由前端重连续播。
STREAM_TERMINAL_EVENTS = {
    "run_completed",
    "run_failed",
    "clarification_required",
    "confirmation_required",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunManager:
    """运行/事件/反馈的持久化与实时分发。线程安全。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: dict[str, asyncio.Queue] = {}
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events (run_id, seq);
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    root_cause TEXT NOT NULL DEFAULT '',
                    solution TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """SSE 所在的事件循环；跨线程推送事件时需要。"""
        self._loop = loop

    def close(self) -> None:
        self._conn.close()

    # --- 运行记录 ---

    def create_run(self, run_id: str, thread_id: str, description: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, thread_id, description, status, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    thread_id,
                    mask_secrets(description),
                    RunStatus.RUNNING,
                    _now(),
                    _now(),
                ),
            )

    def set_status(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
        result_json: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET status = ?, error = ?, result_json = ?, updated_at = ?"
                " WHERE run_id = ?",
                (status, error, result_json, _now(), run_id),
            )

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id, thread_id, description, status, error, result_json,"
                " created_at, updated_at FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row[0],
            "thread_id": row[1],
            "description": row[2],
            "status": row[3],
            "error": row[4],
            "result_json": row[5],
            "created_at": row[6],
            "updated_at": row[7],
        }

    def list_runs(self, limit: int = 50) -> list[dict]:
        """按创建时间倒序列出运行摘要（前端侧栏历史用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, thread_id, description, status, error,"
                " created_at, updated_at FROM runs"
                " ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "run_id": row[0],
                "thread_id": row[1],
                "description": row[2],
                "status": row[3],
                "error": row[4],
                "created_at": row[5],
                "updated_at": row[6],
            }
            for row in rows
        ]

    # --- 事件 ---

    def emit(self, run_id: str, event_type: str, payload: dict) -> None:
        """落库一条事件并推送给该运行的实时队列（如有）。"""
        event = {
            "seq": 0,
            "run_id": run_id,
            "type": event_type,
            "payload": payload,
            "created_at": _now(),
        }
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO events (run_id, type, payload_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (run_id, event_type, json.dumps(payload, ensure_ascii=False), event["created_at"]),
            )
            event["seq"] = cursor.lastrowid
        queue = self._queues.get(run_id)
        if queue is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(queue.put_nowait, event)

    def events_after(self, run_id: str, after_seq: int = 0) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, type, payload_json, created_at FROM events"
                " WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [
            {
                "seq": row[0],
                "run_id": run_id,
                "type": row[1],
                "payload": json.loads(row[2]),
                "created_at": row[3],
            }
            for row in rows
        ]

    def register_queue(self, run_id: str) -> asyncio.Queue:
        """SSE 连接注册实时队列；须在事件循环内调用。"""
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[run_id] = queue
        return queue

    def unregister_queue(self, run_id: str) -> None:
        self._queues.pop(run_id, None)

    # --- 反馈 ---

    def add_feedback(
        self, run_id: str, rating: int, root_cause: str, solution: str
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO feedback (run_id, rating, root_cause, solution, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, rating, root_cause, solution, _now()),
            )


def execute_run(
    graph,
    manager: RunManager,
    run_id: str,
    thread_id: str,
    invoke_input,
) -> None:
    """在工作线程中执行（或恢复）诊断图，落库事件与终态。

    invoke_input 是初始输入字典或 langgraph 的 Command(resume=...)。
    节点通过 config["configurable"]["event_sink"] 上报事件。
    """
    config = {
        "configurable": {
            "thread_id": thread_id,
            "event_sink": lambda event: manager.emit(
                run_id, event["type"], event["payload"]
            ),
        }
    }
    try:
        result = graph.invoke(invoke_input, config=config)
        interrupts = result.get("__interrupt__")
        if interrupts:
            value = interrupts[0].value
            if value.get("type") == "clarification":
                manager.set_status(run_id, RunStatus.WAITING_CLARIFICATION)
                manager.emit(run_id, "clarification_required", value)
            else:
                manager.set_status(run_id, RunStatus.WAITING_CONFIRMATION)
                manager.emit(run_id, "confirmation_required", value)
            return

        result_json = None
        if result.get("diagnosis") is not None:
            result_json = json.dumps(
                _response_dump(run_id, result), ensure_ascii=False
            )
        manager.set_status(
            run_id,
            str(result.get("status", RunStatus.COMPLETED)),
            error=result.get("error"),
            result_json=result_json,
        )
        manager.emit(run_id, "run_completed", {"status": str(result.get("status"))})
    except Exception as exc:  # 执行失败同样写状态与事件（§7）
        manager.set_status(run_id, RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
        manager.emit(run_id, "run_failed", {"error": f"{type(exc).__name__}: {exc}"})


def _response_dump(run_id: str, result: dict) -> dict:
    from fastapi_doctor.domain.models import DiagnosisResponse

    return DiagnosisResponse.model_validate(
        {"run_id": run_id, **result}
    ).model_dump(mode="json")
