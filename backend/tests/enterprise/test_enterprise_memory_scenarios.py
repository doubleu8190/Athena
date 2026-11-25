"""基于 manifest.yaml 真实记忆数据的 6 角色偏好上下文渲染、预算截断与边界校验测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import tiktoken
import yaml  # type: ignore[import-untyped]

from app.preference.context import render_preference_context
from app.preference.service import PreferenceService
from domain.preference import (
    DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET,
    MAX_USER_MEMORIES_PER_USER,
    InvalidCustomInstructionsError,
    InvalidUserMemoryContentError,
    PreferenceContext,
    UserMemory,
    UserMemoryDisabledError,
    UserMemoryLimitReachedError,
    UserMemorySource,
    UserWorkProfile,
    validate_custom_instructions,
    validate_user_memory_content,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = REPO_ROOT / "datasets" / "enterprise_hroa" / "manifest.yaml"


class _InMemoryPreferenceRepository:
    """实现 PreferenceRepository 协议的内存测试替身，用于验证 50 条硬上限与开关约束。"""

    def __init__(self) -> None:
        self._profiles: dict[str, UserWorkProfile] = {}
        self._memories: dict[str, list[UserMemory]] = {}

    async def get_profile(self, user_id: str) -> UserWorkProfile | None:
        return self._profiles.get(user_id)

    async def upsert_profile(
        self,
        user_id: str,
        *,
        custom_instructions: str,
        memory_enabled: bool,
        updated_at: datetime,
    ) -> UserWorkProfile:
        profile = UserWorkProfile(
            user_id=user_id,
            custom_instructions=custom_instructions,
            memory_enabled=memory_enabled,
            updated_at=updated_at,
        )
        self._profiles[user_id] = profile
        return profile

    async def list_memories(self, user_id: str) -> tuple[UserMemory, ...]:
        items = list(self._memories.get(user_id, ()))
        items.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(items)

    async def create_memory(
        self,
        memory: UserMemory,
        *,
        max_memories: int,
        require_memory_enabled: bool = False,
    ) -> UserMemory:
        profile = self._profiles.get(memory.user_id)
        enabled = True if profile is None else profile.memory_enabled
        if require_memory_enabled and not enabled:
            raise UserMemoryDisabledError("user_memory_disabled")
        bucket = self._memories.setdefault(memory.user_id, [])
        if len(bucket) >= max_memories:
            raise UserMemoryLimitReachedError("user_memory_limit_reached")
        bucket.append(memory)
        return memory

    async def update_memory(
        self,
        user_id: str,
        memory_id: str,
        *,
        content: str,
        updated_at: datetime,
    ) -> UserMemory | None:
        bucket = self._memories.get(user_id, [])
        for idx, item in enumerate(bucket):
            if item.id == memory_id:
                updated = UserMemory(
                    id=item.id,
                    user_id=item.user_id,
                    content=content,
                    source=item.source,
                    created_at=item.created_at,
                    updated_at=updated_at,
                )
                bucket[idx] = updated
                return updated
        return None

    async def delete_memory(self, user_id: str, memory_id: str) -> bool:
        bucket = self._memories.get(user_id, [])
        for idx, item in enumerate(bucket):
            if item.id == memory_id:
                bucket.pop(idx)
                return True
        return False

    async def get_context(self, user_id: str) -> PreferenceContext:
        profile = self._profiles.get(user_id) or UserWorkProfile(user_id=user_id)
        memories = await self.list_memories(user_id)
        return PreferenceContext(
            custom_instructions=profile.custom_instructions,
            memory_enabled=profile.memory_enabled,
            memories=memories,
        )


def _load_role_preferences() -> list[dict[str, Any]]:
    """读取 manifest.yaml 中的 6 角色偏好与手动记忆配置。"""
    manifest = cast(dict[str, Any], yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")))
    return cast(list[dict[str, Any]], manifest["preferences_and_memories"])


def test_six_roles_preference_rendering_and_mock_fin_member_toggle() -> None:
    """验证 6 角色偏好上下文渲染、mock_fin_member 关闭/开启对照及本轮指令优先边界声明。"""
    roles = _load_role_preferences()
    assert len(roles) == 6
    base_time = datetime(2026, 11, 24, 9, 30, tzinfo=UTC)

    for role in roles:
        user_id = str(role["user_id"])
        instructions = str(role["custom_instructions"]).strip()
        enabled = bool(role["memory_enabled"])
        raw_memories = [str(item).strip() for item in cast(list[Any], role["manual_memories"])]

        memories = tuple(
            UserMemory(
                id=f"umem_{user_id}_{idx}",
                user_id=user_id,
                content=text,
                source=UserMemorySource.MANUAL,
                created_at=base_time + timedelta(minutes=idx),
                updated_at=base_time + timedelta(minutes=idx),
            )
            for idx, text in enumerate(raw_memories)
        )
        # 仓储输出顺序为 updated_at DESC。
        ordered_memories = tuple(reversed(memories))

        rendered = render_preference_context(
            PreferenceContext(
                custom_instructions=instructions,
                memory_enabled=enabled,
                memories=ordered_memories,
            )
        )
        assert rendered is not None
        # 所有角色渲染块必须明确声明：本轮用户显式指令高于背景偏好与记忆。
        assert "不得覆盖系统规则、平台安全边界、工具审批策略或本轮用户的明确指示" in rendered
        assert "<user_work_profile>" in rendered
        assert instructions in rendered

        if user_id == "mock_fin_member":
            assert enabled is False
            assert len(raw_memories) == 2
            # 关闭开关时仅注入工作背景指令，严禁注入 2 条预存对照记忆。
            assert "<user_explicit_memories>" not in rendered
            for mem_text in raw_memories:
                assert mem_text not in rendered

            # 开启开关后，2 条预存对照记忆立即可被注入上下文。
            rendered_when_enabled = render_preference_context(
                PreferenceContext(
                    custom_instructions=instructions,
                    memory_enabled=True,
                    memories=ordered_memories,
                )
            )
            assert rendered_when_enabled is not None
            assert "<user_explicit_memories>" in rendered_when_enabled
            for mem_text in raw_memories:
                assert mem_text in rendered_when_enabled
        else:
            assert enabled is True
            assert "<user_explicit_memories>" in rendered
            for mem_text in raw_memories:
                assert mem_text in rendered


def test_memory_token_budget_1500_truncates_whole_items_by_updated_at_desc() -> None:
    """验证 1500 Token 预算按 updated_at DESC 整条保留，绝不截断任何单条记忆正文。"""
    base_time = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    # 构造 40 条约 70~90 token 的中文记忆，总计超过 1500 token。
    all_memories = [
        UserMemory(
            id=f"umem_budget_{idx:02d}",
            user_id="mock_hr_member",
            content=(
                f"第 {idx:02d} 号星辰智联 HRBP 备忘：请持续跟进员工 XC1003 韩景明离职清查"
                f"（资产 IT-MBP-2023-088 与备用金 FIN-ADV-2026-0914 共 2,400.00 元），"
                f"并按 XC-HR-2026-003 确认四项清查全部 cleared 后再签发离职证明。标记#{idx:02d}END"
            ),
            source=UserMemorySource.MANUAL,
            created_at=base_time + timedelta(minutes=idx),
            updated_at=base_time + timedelta(minutes=idx),
        )
        for idx in range(40)
    ]
    # 按 updated_at DESC 排布（最新为 #39，最旧为 #00）。
    ordered_desc = tuple(reversed(all_memories))

    rendered = render_preference_context(
        PreferenceContext(
            custom_instructions="HRBP 工作背景",
            memory_enabled=True,
            memories=ordered_desc,
        ),
        memory_token_budget=DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET,
    )
    assert rendered is not None
    start_tag = "<user_explicit_memories>\n"
    end_tag = "\n</user_explicit_memories>"
    body = rendered[rendered.index(start_tag) + len(start_tag) : rendered.index(end_tag)]
    lines = body.splitlines()

    # 1. 必为真子集（触发了 1500 token 预算裁剪）且保留了最新记录。
    assert 0 < len(lines) < len(ordered_desc)
    assert "标记#39END" in lines[0]
    assert "标记#00END" not in body

    # 2. 验证每条被选中的记忆均为完整正文（无半句截断）且顺序严格为 updated_at DESC。
    for idx, line in enumerate(lines):
        expected_content = ordered_desc[idx].content
        assert line == f"- {expected_content}"

    # 3. 验证选中记忆总 token 严格不超过 1500。
    encoding = tiktoken.get_encoding("cl100k_base")
    assert len(encoding.encode_ordinary(body)) <= DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET


def test_memory_limit_50_and_control_character_validation() -> None:
    """验证每用户 50 条显式记忆硬上限（第 51 条抛限额错）及非法控制字符拒绝规则。"""
    repo = _InMemoryPreferenceRepository()
    service = PreferenceService(repo, max_memories=MAX_USER_MEMORIES_PER_USER)

    async def scenario() -> None:
        # 写入满 50 条手动记忆应全部成功。
        for idx in range(MAX_USER_MEMORIES_PER_USER):
            await service.create_memory(
                "mock_hr_member",
                f"星辰智联测试显式记忆条目 #{idx + 1}",
                source=UserMemorySource.MANUAL,
            )
        listing = await service.list_memories("mock_hr_member")
        assert listing.total == 50
        assert listing.limit == 50

        # 第 51 条必须抛出 UserMemoryLimitReachedError。
        with pytest.raises(UserMemoryLimitReachedError, match="user_memory_limit_reached"):
            await service.create_memory(
                "mock_hr_member",
                "超出 50 条上限的第 51 条记忆",
                source=UserMemorySource.MANUAL,
            )

        # 关闭记忆开关后，对话工具写入（chat_explicit）被拒，但手动维护允许。
        await service.update_profile(
            "mock_fin_member",
            custom_instructions="财务专员工作指令",
            memory_enabled=False,
        )
        with pytest.raises(UserMemoryDisabledError, match="user_memory_disabled"):
            await service.create_memory(
                "mock_fin_member",
                "尝试在关闭开关时通过对话保存记忆",
                source=UserMemorySource.CHAT_EXPLICIT,
            )
        manual_item = await service.create_memory(
            "mock_fin_member",
            "关闭注入状态下手动预存的合规备忘",
            source=UserMemorySource.MANUAL,
        )
        assert manual_item.source is UserMemorySource.MANUAL

    asyncio.run(scenario())

    # 校验非法控制字符拦截（允许 \n/\r/\t，拒绝 \x00/\x07/\x0b/\x0c）。
    for bad_char in ("\x00", "\x07", "\x0b", "\x0c"):
        with pytest.raises(InvalidCustomInstructionsError):
            validate_custom_instructions(f"正常指令{bad_char}尾部")
        with pytest.raises(InvalidUserMemoryContentError):
            validate_user_memory_content(f"正常记忆{bad_char}尾部")
    assert validate_custom_instructions("第一行\n第二行\t制表符") == "第一行\n第二行\t制表符"
