"""Worker Job 的 JSON 字段边界回归测试。"""

from __future__ import annotations

from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    _load_json_string_list,
)


def test_worker_allowed_tools_json_array_is_preserved():
    """JSON 数组白名单必须保留，不能退化为空工具集合。"""
    assert _load_json_string_list(["exec_shell"]) == ("exec_shell",)
    assert _load_json_string_list([]) == ()


def test_worker_allowed_tools_json_invalid_values_fail_closed():
    """格式错误或非字符串成员不得扩大 Worker 能力。"""
    assert _load_json_string_list("not-json") == ()
    assert _load_json_string_list({"name": "exec_shell"}) == ()
    assert _load_json_string_list(["exec_shell", 1]) == ()
