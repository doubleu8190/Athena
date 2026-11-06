"""工具目录分页对象。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolPage[ToolItem]:
    """工具目录的一页结果及总条目数。"""

    items: tuple[ToolItem, ...]
    total: int


__all__ = ["ToolPage"]
