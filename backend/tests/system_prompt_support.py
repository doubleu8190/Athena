"""系统提示词相关单元测试的固定版本仓储。"""

from __future__ import annotations

from datetime import UTC, datetime

from domain.system_prompt import (
    INITIAL_COMPRESSION_SYSTEM_PROMPT,
    INITIAL_MAIN_SYSTEM_PROMPT,
    INITIAL_SYSTEM_PROMPT_IDS,
    SystemPromptKind,
    SystemPromptVersion,
    SystemPromptVersionPage,
)

TEST_MAIN_PROMPT_ID = INITIAL_SYSTEM_PROMPT_IDS["main"]
TEST_COMPRESSION_PROMPT_ID = INITIAL_SYSTEM_PROMPT_IDS["compression"]
TEST_MAIN_PROMPT = INITIAL_MAIN_SYSTEM_PROMPT
TEST_COMPRESSION_PROMPT = INITIAL_COMPRESSION_SYSTEM_PROMPT


def prompt_version(
    kind: SystemPromptKind,
    *,
    version_id: str | None = None,
    content: str | None = None,
) -> SystemPromptVersion:
    """构造测试使用的不可变提示词版本。"""
    initial_id = INITIAL_SYSTEM_PROMPT_IDS[kind.value]
    return SystemPromptVersion(
        id=version_id or initial_id,
        kind=kind,
        content=content
        or (
            TEST_MAIN_PROMPT
            if kind is SystemPromptKind.MAIN
            else TEST_COMPRESSION_PROMPT
        ),
        created_by=None,
        created_at=datetime(2026, 11, 11, tzinfo=UTC),
        restored_from_version_id=None,
    )


class FakeSystemPromptRepository:
    """只返回测试绑定版本的只读系统提示词仓储。"""

    def __init__(
        self,
        *,
        main_content: str = TEST_MAIN_PROMPT,
        compression_content: str = TEST_COMPRESSION_PROMPT,
    ) -> None:
        """保存主提示词与压缩提示词测试正文。"""
        self.main = prompt_version(
            SystemPromptKind.MAIN,
            content=main_content,
        )
        self.compression = prompt_version(
            SystemPromptKind.COMPRESSION,
            content=compression_content,
        )

    async def get_current(self, kind: SystemPromptKind) -> SystemPromptVersion | None:
        """按类别返回固定当前版本。"""
        return self.main if kind is SystemPromptKind.MAIN else self.compression

    async def list_versions(
        self,
        kind: SystemPromptKind,
        *,
        page: int,
        page_size: int,
    ) -> SystemPromptVersionPage:
        """返回仅含当前版本的一页历史。"""
        item = await self.get_current(kind)
        return SystemPromptVersionPage(
            items=() if item is None else (item,),
            total=0 if item is None else 1,
            page=page,
            page_size=page_size,
        )

    async def get_version(
        self, kind: SystemPromptKind, version_id: str
    ) -> SystemPromptVersion | None:
        """按类别和标识返回固定版本。"""
        item = await self.get_current(kind)
        return item if item is not None and item.id == version_id else None

    async def save(self, *args: object, **kwargs: object) -> SystemPromptVersion:
        """单元测试不支持写操作。"""
        raise AssertionError("save_not_supported")

    async def restore(self, *args: object, **kwargs: object) -> SystemPromptVersion:
        """单元测试不支持恢复操作。"""
        raise AssertionError("restore_not_supported")

    async def get_by_id(self, version_id: str) -> SystemPromptVersion | None:
        """按标识返回主提示词或压缩提示词固定版本。"""
        for item in (self.main, self.compression):
            if item.id == version_id:
                return item
        return None

    async def get_pair_by_ids(
        self, main_version_id: str, compression_version_id: str
    ) -> tuple[SystemPromptVersion, SystemPromptVersion] | None:
        """仅当两个标识与固定版本一致时返回本对正文。"""
        if (
            main_version_id != self.main.id
            or compression_version_id != self.compression.id
        ):
            return None
        return self.main, self.compression


__all__ = [
    "FakeSystemPromptRepository",
    "TEST_COMPRESSION_PROMPT",
    "TEST_COMPRESSION_PROMPT_ID",
    "TEST_MAIN_PROMPT",
    "TEST_MAIN_PROMPT_ID",
    "prompt_version",
]
