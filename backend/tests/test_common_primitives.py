"""基础共享原语（UNSET 单例、utc_now 与 common.ids 边界）单元测试。"""

from __future__ import annotations

from datetime import UTC

import common.ids as common_ids
from common.date import utc_now
from common.unset import UNSET, UnsetType
from domain.llm.model import ModelProfileUpdateInput
from domain.subagent import UpdateSubagentCommand
from domain.tool import ToolMetadataPatch


def test_unset_sentinel_is_process_wide_singleton_across_domains() -> None:
    """全系统各领域的局部更新哨兵必须共享同一个 common.unset.UNSET 单例。"""
    assert isinstance(UNSET, UnsetType)
    assert UnsetType.TOKEN is UNSET
    assert UnsetType("UNSET") is UNSET
    assert repr(UNSET) == "UNSET"

    profile_patch = ModelProfileUpdateInput()
    assert profile_patch.name is UNSET
    assert profile_patch.api_key is UNSET
    assert profile_patch.has_updates() is False

    # 显式传入 None 与 UNSET 必须严格区分。
    profile_clear = ModelProfileUpdateInput(temperature=None)
    assert profile_clear.temperature is None
    assert profile_clear.temperature is not UNSET
    assert profile_clear.has_updates() is True

    tool_patch = ToolMetadataPatch()
    assert tool_patch.requires_approval is UNSET
    assert tool_patch.display_name is UNSET
    assert tool_patch.has_updates() is False

    tool_clear = ToolMetadataPatch(display_name=None)
    assert tool_clear.display_name is None
    assert tool_clear.display_name is not UNSET
    assert tool_clear.has_updates() is True

    subagent_cmd = UpdateSubagentCommand(expected_version=1)
    assert subagent_cmd.slug is UNSET
    assert subagent_cmd.owner_department_id is UNSET
    assert isinstance(subagent_cmd.slug, UnsetType)


def test_common_ids_no_longer_exports_now_utc_alias() -> None:
    """统一收敛至 common.date.utc_now 后，common.ids 不再暴露旧兼容别名。"""
    assert not hasattr(common_ids, "now_utc")
    assert "now_utc" not in common_ids.__all__
    generated = common_ids.new_id("test")
    assert generated.startswith("test_")
    current = utc_now()
    assert current.tzinfo is UTC
