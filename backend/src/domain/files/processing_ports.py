"""文件适配器选择、解析及异步处理端口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from domain.leases import LeaseGrant

from .entities import AdapterInfo, Attachment, FileMetadata


class FileAdapterRegistryPort(Protocol):
    """文件名/MIME 到解析适配器的选择能力。"""

    def select(self, filename: str, mime_type: str) -> AdapterInfo: ...
    def supported_extensions(self) -> list[str]: ...


class DocumentJobPort(Protocol):
    """文档处理任务入队能力。"""

    async def enqueue(self, attachment_id: str) -> bool: ...
    async def cancel(self, attachment_id: str) -> None: ...


class DocumentJobQueue(Protocol):
    """文档处理 worker 使用的任务领取和完成能力。"""

    async def claim_next(self, *, max_attempts: int = 5, lease_seconds: float = 60.0) -> dict[str, object] | None: ...
    async def mark_succeeded(self, job_id: str, result: dict[str, object], *, lease: LeaseGrant) -> None: ...
    async def mark_failed(self, job_id: str, error: str, *, retry: bool, lease: LeaseGrant) -> None: ...


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """解析器输出的规范化文档。"""

    text: str
    metadata: FileMetadata = field(default_factory=FileMetadata)
    adapter_name: str | None = None
    adapter_version: str | None = None
    capabilities: tuple[str, ...] = ()


class DocumentParserPort(Protocol):
    """将附件解析为规范化文档的能力。"""

    async def parse(self, attachment: Attachment) -> ParsedDocument: ...


__all__ = [
    "DocumentJobPort",
    "DocumentJobQueue",
    "DocumentParserPort",
    "FileAdapterRegistryPort",
    "ParsedDocument",
]
