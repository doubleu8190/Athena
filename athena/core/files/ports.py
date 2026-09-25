"""文件智能依赖的存储端口。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class FileVectorStore(Protocol):
    """文件分块向量索引的存储无关端口。"""

    async def replace_attachment(
        self,
        attachment_id: str,
        chunks: Sequence[Mapping[str, Any]],
    ) -> None:
        """用附件当前分块替换其向量记录。"""
        ...

    async def delete_attachment(self, attachment_id: str) -> None:
        """删除附件的全部向量记录。"""
        ...

    async def query(
        self,
        query: str,
        limit: int,
        where: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """查询向量候选并返回统一的领域字典。"""
        ...
