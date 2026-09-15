from fastapi_doctor.domain.models import RunStatus
from fastapi_doctor.graph.builder import build_diagnosis_graph


def invoke_graph(description: str, logs: str = "") -> dict:
    return build_diagnosis_graph().invoke(
        {
            "run_id": "test-run",
            "description": description,
            "logs": logs,
            "code": "",
            "config": "",
            "status": RunStatus.RUNNING,
        }
    )


def test_complete_input_reaches_plan() -> None:
    result = invoke_graph(
        "FastAPI returns 422 when creating a user",
        "422 Unprocessable Entity",
    )

    assert result["status"] == RunStatus.PLANNED
    assert result["fault_info"].framework == "fastapi"
    assert result["fault_info"].http_status == 422
    assert result["fault_info"].component == "http_api"
    assert result["clarification_questions"] == []
    assert "422" in result["plan"].search_queries[0]


def test_missing_context_stops_for_clarification() -> None:
    result = invoke_graph("接口出错了")

    assert result["status"] == RunStatus.NEEDS_CLARIFICATION
    assert result["plan"] if "plan" in result else None is None
    assert len(result["clarification_questions"]) == 3

