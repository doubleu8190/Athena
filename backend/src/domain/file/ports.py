"""会话文件持久化与私有对象存储端口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Protocol

from domain.file.model import (
    FileObjectScanEntry,
    PendingSessionFile,
    SessionFile,
    SessionFilePage,
)


class SessionFileRepository(Protocol):
    """会话文件元数据的事务边界。"""

    async def publish_upload(
        self,
        session_id: str,
        owner_id: str,
        file: PendingSessionFile,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        published_at: datetime,
    ) -> SessionFile:
        """在会话配额锁内发布上传；同一请求键只返回原文件。"""

    async def find_upload_request(
        self,
        session_id: str,
        owner_id: str,
        idempotency_key: str,
    ) -> SessionFile | None:
        """在写入对象前读取已提交的同键上传。"""

    async def list_visible(
        self,
        session_id: str,
        owner_id: str,
        *,
        after_file_id: str | None,
        limit: int,
    ) -> SessionFilePage | None:
        """按会话作用域和稳定游标读取文件；不可见会话返回 None。"""

    async def get_visible(
        self,
        session_id: str,
        owner_id: str,
        file_id: str,
    ) -> SessionFile | None:
        """读取同一未删除会话内的文件。"""

    async def get_visible_many(
        self,
        session_id: str,
        owner_id: str,
        file_ids: tuple[str, ...],
    ) -> tuple[SessionFile, ...]:
        """一次读取并保持请求顺序；任一文件不可见即由调用方拒绝。"""


class FileObjectReferenceRepository(Protocol):
    """在离线停写窗口内复核数据库中的对象引用。"""

    async def is_object_key_referenced(self, object_key: str) -> bool:
        """复核会话文件、Skill 版本和非空执行日志三处引用。"""


class FileObjectStore(Protocol):
    """技能包、会话文件和执行日志共用的私有不可变对象存储。"""

    async def start(self) -> None:
        """校验存储配置和 bucket 可用性。"""

    async def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str,
    ) -> None:
        """从临时文件流式写入不可变对象。"""

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        """写入有界内存内容。"""

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        """读取不超过调用方上限的对象。"""

    async def get_range(self, key: str, *, offset: int, length: int) -> bytes:
        """读取精确字节区间，供严格 UTF-8 分页使用。"""

    def iter_range(
        self,
        key: str,
        *,
        offset: int = 0,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        """从指定偏移分块读取对象，直到 EOF。"""

    async def delete(self, key: str) -> None:
        """幂等删除不可见孤立对象。"""

    def iter_maintenance_candidates(
        self,
        *,
        older_than_seconds: int = 86400,
        after_key: str | None = None,
        maximum_items: int = 1000,
    ) -> AsyncIterator[FileObjectScanEntry]:
        """从单次命令的分页位置返回对象并标记合法年龄候选。"""

    async def aclose(self) -> None:
        """释放线程池和 HTTP 连接。"""


__all__ = [
    "FileObjectReferenceRepository",
    "FileObjectStore",
    "SessionFileRepository",
]
