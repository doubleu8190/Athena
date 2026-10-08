"""文件解析产物及适配器注册表的持久化端口。"""

from __future__ import annotations

from typing import Protocol

from .entities import AdapterInfo, FileArtifact


class FileArtifactRepository(Protocol):
    """文件解析产物持久化能力。"""

    async def get(self, cache_key: str) -> FileArtifact | None: ...
    async def put(self, artifact: FileArtifact) -> FileArtifact: ...


class AdapterRegistryRepository(Protocol):
    """文件适配器注册表持久化能力。"""

    async def replace_all(self, adapters: list[AdapterInfo]) -> None: ...
    async def list_adapters(self) -> list[AdapterInfo]: ...


__all__ = ["AdapterRegistryRepository", "FileArtifactRepository"]
