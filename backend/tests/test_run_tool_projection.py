"""工具调用详情的技能身份脱敏回归。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from api.converter.run import tool_call_to_response
from domain.skill import (
    SKILL_EXECUTE_TOOL_ID,
    SKILL_LOAD_TOOL_ID,
    SKILL_READ_RESOURCE_TOOL_ID,
)
from domain.tool import (
    RunToolCallDetails,
    RunToolCallRecord,
    ToolCallStatus,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
)

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def _details(
    tool_id: str | None,
    model_name: str = "opaque_model_name",
) -> RunToolCallDetails:
    """构造带敏感参数和内部结果数据的已完成调用。"""
    record_id = tool_id or "none"
    record = RunToolCallRecord(
        id=f"record_{record_id}",
        run_id="run_one",
        session_id="session_one",
        tool_call_id=f"call_{record_id}",
        ordinal=1,
        tool_id=tool_id,
        tool_kind=ToolKind.BUILTIN,
        model_name=model_name,
        execution_epoch=1,
        status=ToolCallStatus.COMPLETED,
        call_message_id="message_call",
        result_message_id="message_result",
        started_at=NOW,
        finished_at=NOW,
        safe_error_code=None,
        outcome=ToolOutcome(
            status=ToolOutcomeStatus.OK,
            code="skill_executed",
            message="ok",
            data={"internal_path": "/host/private", "password": "secret"},
        ),
    )
    return RunToolCallDetails(
        record,
        {"internal_path": "/host/private", "password": "secret"},
        "private log",
    )


@pytest.mark.parametrize(
    "tool_id",
    (
        SKILL_EXECUTE_TOOL_ID,
        SKILL_LOAD_TOOL_ID,
        SKILL_READ_RESOURCE_TOOL_ID,
    ),
)
def test_all_skill_tool_details_hide_arguments_and_outcome_data(tool_id: str) -> None:
    response = tool_call_to_response(_details(tool_id))

    assert response.arguments is None
    assert response.outcome is not None
    assert response.outcome.data == {}
    assert "private" not in response.model_dump_json()
    assert "secret" not in response.model_dump_json()


@pytest.mark.parametrize(
    ("tool_id", "model_name"),
    (
        (None, "skill_execute"),
        ("tool_public", "skill_load"),
        ("tool_public", "  CUSTOM_SKILL_READ_RESOURCE  "),
    ),
)
def test_skill_model_names_are_redacted_without_trusting_tool_id(
    tool_id: str | None,
    model_name: str,
) -> None:
    """tool_id 缺失、冲突或带前缀时，仍必须按 modelName 隐藏 Skill 详情。"""
    response = tool_call_to_response(_details(tool_id, model_name))

    assert response.arguments is None
    assert response.outcome is not None
    assert response.outcome.data == {}
    assert "private" not in response.model_dump_json()
    assert "secret" not in response.model_dump_json()


def test_non_skill_tool_detail_keeps_existing_projection() -> None:
    """统一判别不能误伤普通工具详情。"""
    response = tool_call_to_response(_details("tool_public", "public_tool"))

    assert response.arguments == {
        "internal_path": "/host/private",
        "password": "secret",
    }
    assert response.outcome is not None
    assert response.outcome.data["internal_path"] == "/host/private"
