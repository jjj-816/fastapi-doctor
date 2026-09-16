"""运行评测集（设计 §8.2/§8.3）：真实索引 + 真实模型，输出各项指标。

用法：
    .venv/Scripts/python.exe scripts/run_eval.py
    .venv/Scripts/python.exe scripts/run_eval.py --eval-set data/eval/eval_set.json --limit 3

评测集 JSON 不入库（data/ 为本地资料）；脚本本身是代码，随仓库提交。
指标为设计目标而非发布门槛，结果与失败案例如实记录并保存到
data/eval/results/ 下供失败分析。
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from fastapi_doctor.domain.models import RunStatus
from fastapi_doctor.graph.builder import build_diagnosis_graph
from fastapi_doctor.llm import build_llm
from fastapi_doctor.retrieval.retriever import KnowledgeRetriever

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_EVAL_SET = BASE_DIR / "data" / "eval" / "eval_set.json"
RESULTS_DIR = BASE_DIR / "data" / "eval" / "results"
TOP_K = 5


def evaluate_record(graph, record: dict) -> dict:
    """对单条记录跑完整图，并计算可自动判定的指标。"""
    expected = record["expected"]
    result: dict = {"id": record["id"], "category": record["category"]}
    t0 = time.time()
    events: list[dict] = []
    try:
        state = graph.invoke(
            {
                "run_id": f"eval-{record['id']}",
                "description": record["description"],
                "logs": record["logs"],
                "code": "",
                "config": "",
                "status": RunStatus.RUNNING,
            },
            config={
                "configurable": {
                    "thread_id": f"eval-{record['id']}",
                    "event_sink": events.append,
                }
            },
        )
    except Exception as exc:  # 图执行失败本身就是一条指标
        result.update(
            execution_ok=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed=round(time.time() - t0, 1),
        )
        return result

    result["elapsed"] = round(time.time() - t0, 1)
    result["execution_ok"] = True

    # 检索计划入库（规划器 + 每轮检索词），Hit@5 未命中时可直接归因。
    plans = [e["payload"] for e in events if e["type"] == "plan_created"]
    if plans:
        result["plans"] = plans

    # 中断即两类人工介入（§4.2/§4.6）：澄清暂停、危险确认暂停。
    interrupts = state.get("__interrupt__")
    if interrupts:
        value = interrupts[0].value
        if value.get("type") == "clarification":
            result["status"] = "waiting_clarification"
            result["clarified"] = True
            result["clarification_ok"] = expected["should_clarify"]
        else:
            result["status"] = "waiting_confirmation"
            result["clarification_ok"] = not expected["should_clarify"]
            result["confirmation_ok"] = expected["expect_confirmation"]
            # 记录触发确认的建议与审查问题：能区分真实拦截与误触发。
            diagnosis = state.get("diagnosis")
            if diagnosis is not None:
                result["fix_suggestions"] = diagnosis.fix_suggestions
            review = state.get("review")
            if review is not None:
                result["review_issues"] = review.issues
        return result

    status = state["status"]
    result["status"] = str(status)
    result["clarified"] = False
    result["clarification_ok"] = not expected["should_clarify"]
    result["needs_confirmation"] = False
    result["confirmation_ok"] = (
        False if expected["expect_confirmation"] else None
    )

    # Retrieval Hit@5：必要来源是否出现在前 5 条证据的 doc_id 中。
    doc_ids = [e.doc_id for e in state.get("evidence", [])]
    top5 = doc_ids[:TOP_K]
    must = expected["must_retrieve"]
    result["top5_doc_ids"] = top5
    result["hit5"] = any(doc in top5 for doc in must) if must else None

    # 根因命中：根因关键词出现在主结论或修复建议里（大小写不敏感）。
    diagnosis = state.get("diagnosis")
    if diagnosis is not None and expected["root_cause_keywords"]:
        text = " ".join(
            [diagnosis.most_likely_cause, *diagnosis.fix_suggestions]
        ).lower()
        result["root_cause_hit"] = any(
            kw.lower() in text for kw in expected["root_cause_keywords"]
        )
        result["most_likely_cause"] = diagnosis.most_likely_cause
    else:
        result["root_cause_hit"] = None

    # 引用完整率：给出了引用，且没有引用不存在的证据。
    if diagnosis is not None:
        review = state.get("review")
        unknown = bool(review) and any("不存在" in i for i in review.issues)
        result["citations_complete"] = bool(diagnosis.citations) and not unknown
    else:
        result["citations_complete"] = None
    return result


def summarize(results: list[dict]) -> dict:
    """汇总设计 §8.3 的各项目标指标（None 表示该题不适用）。"""
    total = len(results)
    executed = [r for r in results if r.get("execution_ok")]

    def rate(key: str) -> float | None:
        values = [r[key] for r in results if r.get(key) is not None]
        return round(sum(values) / len(values), 2) if values else None

    return {
        "total": total,
        "graph_execution_success": (
            round(len(executed) / total, 2) if total else None
        ),
        "clarification_accuracy": rate("clarification_ok"),
        "retrieval_hit_at_5": rate("hit5"),
        "root_cause_hit": rate("root_cause_hit"),
        "citations_complete": rate("citations_complete"),
        "dangerous_confirmation": rate("confirmation_ok"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 FastAPI Doctor 评测集")
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（调试用）")
    args = parser.parse_args()

    records = json.loads(args.eval_set.read_text(encoding="utf-8"))["records"]
    if args.limit:
        records = records[: args.limit]

    graph = build_diagnosis_graph(retriever=KnowledgeRetriever(), llm=build_llm())

    results = []
    for record in records:
        print(f"运行 {record['id']}（{record['category']}）...", flush=True)
        results.append(evaluate_record(graph, record))

    summary = summarize(results)

    print("\n===== 逐题结果 =====")
    for r in results:
        if not r.get("execution_ok"):
            print(f"{r['id']}  执行失败: {r['error']}")
            continue

        def mark(key: str) -> str:
            value = r.get(key)
            return "✓" if value else ("✗" if value is not None else "—")

        print(
            f"{r['id']}  status={r.get('status', '?'):<24}"
            f"  澄清={'✓' if r.get('clarification_ok') else '✗'}"
            f"  Hit@5={mark('hit5')}"
            f"  根因={mark('root_cause_hit')}"
            f"  引用={mark('citations_complete')}"
            f"  确认={mark('confirmation_ok')}"
            f"  [{r.get('elapsed', '?')}s]"
        )
        if r.get("most_likely_cause"):
            print(f"      主因: {r['most_likely_cause']}")

    print("\n===== 汇总（设计 §8.3 目标值：澄清≥0.80 Hit@5≥0.80 根因≥0.75 引用≥0.90 拦截=1.00）=====")
    for key, value in summary.items():
        print(f"{key}: {value}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"eval_{stamp}.json"
    out_path.write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()
