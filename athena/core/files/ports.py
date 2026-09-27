"""文件智能依赖的存储端口。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .contracts import FileRetrievalCandidate


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
    ) -> list[FileRetrievalCandidate]:
        """查询向量候选并返回强类型文件候选。"""
        ...


class FileReranker(Protocol):
    """文件检索候选的独立重排端口。"""

    async def initialize(self) -> None:
        """加载重排模型。"""
        ...

    async def rerank(
        self,
        query: str,
        candidates: Sequence[FileRetrievalCandidate],
        limit: int,
    ) -> list[FileRetrievalCandidate]:
        """按查询相关性重排候选。"""
        ...
