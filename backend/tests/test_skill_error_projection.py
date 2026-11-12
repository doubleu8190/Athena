"""Skill 异常正文不得进入 ToolOutcome、模型结果或用户安全码。"""

from __future__ import annotations

from app.skill.errors import public_skill_error_code
from app.skill.execution_service import _safe_preparation_error
from app.tool.runtime import _skill_preparation_error, _skill_resource_error


class _LeakyError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("postgresql://secret@db/internal/path")
        self.code = code


def test_unknown_skill_exception_uses_fixed_safe_projection() -> None:
    error = _LeakyError("postgresql://secret@db/internal/path")

    tool_outcome = _skill_preparation_error(error)
    execution_result = _safe_preparation_error(error)

    assert tool_outcome.code == "skill_preparation_failed"
    assert execution_result.outcome.code == "skill_preparation_failed"
    serialized = (
        tool_outcome.canonical_output()
        + "\n"
        + execution_result.outcome.canonical_output()
    )
    assert "postgresql://" not in serialized
    assert "secret" not in serialized
    assert "/internal/path" not in serialized


def test_unexpected_value_error_text_never_becomes_public_code() -> None:
    error = ValueError("sqlalchemy://user:password@db/path")

    assert (
        public_skill_error_code(error, fallback="skill_preparation_failed")
        == "skill_preparation_failed"
    )
    assert _safe_preparation_error(error).outcome.code == "skill_preparation_failed"


def test_known_skill_business_code_remains_projectable() -> None:
    error = _LeakyError("skill_not_in_run_snapshot")

    assert _skill_preparation_error(error).code == "skill_not_in_run_snapshot"
    assert _safe_preparation_error(error).outcome.code == "skill_not_in_run_snapshot"


def test_skill_resource_unknown_value_error_uses_fixed_safe_code() -> None:
    """资源读取的未知 ValueError 不得把 SQL 或对象路径带入模型结果。"""
    error = ValueError(
        "SELECT * FROM internal_files WHERE password='secret' "
        "postgresql://secret@db/internal/path"
    )

    outcome = _skill_resource_error(error)

    assert outcome.code == "skill_resource_unavailable"
    serialized = outcome.canonical_output()
    assert "SELECT" not in serialized
    assert "postgresql://" not in serialized
    assert "secret" not in serialized
    assert "/internal/path" not in serialized


def test_skill_resource_known_business_code_remains_projectable() -> None:
    """已登记的固定资源错误码仍应保留，便于前端给出明确提示。"""
    error = ValueError("skill_resource_range_invalid")

    outcome = _skill_resource_error(error)

    assert outcome.code == "skill_resource_range_invalid"
    assert outcome.message == "资源读取范围无效。"
