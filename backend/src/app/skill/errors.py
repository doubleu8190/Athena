"""Skill 准备阶段对用户和模型公开的稳定错误码。"""

from __future__ import annotations

_PUBLIC_SKILL_ERROR_CODES = frozenset(
    {
        "session_file_ids_duplicate",
        "session_file_not_found",
        "session_file_runtime_unavailable",
        "skill_dependency_environment_unavailable",
        "skill_dependency_python_invalid",
        "skill_dependency_unsatisfied",
        "skill_not_in_run_snapshot",
        "skill_parameters_invalid",
        "skill_resource_not_found",
        "skill_resource_not_text",
        "skill_resource_offset_invalid",
        "skill_resource_range_invalid",
        "skill_resource_too_large",
        "skill_script_not_registered",
        "skill_version_unavailable",
    }
)


def public_skill_error_code(
    error: BaseException,
    *,
    fallback: str,
) -> str:
    """只保留显式业务错误码，未知异常正文永不进入公开投影。"""
    explicit = getattr(error, "code", None)
    if isinstance(explicit, str) and explicit in _PUBLIC_SKILL_ERROR_CODES:
        return explicit
    # 领域层部分 ValueError 直接携带固定码，只认可白名单中的文本。
    if isinstance(error, ValueError) and str(error) in _PUBLIC_SKILL_ERROR_CODES:
        return str(error)
    return fallback


__all__ = ["public_skill_error_code"]
