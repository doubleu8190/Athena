"""工具使用统计查询端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ToolUsageQueryPort(Protocol):
    async def last_called_by_tool(self) -> dict[str, str]: ...
    async def count_calls_since(self, since: datetime) -> int: ...


__all__ = ["ToolUsageQueryPort"]
