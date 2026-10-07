"""阶段 7 Memory 领域、应用服务和旧实现适配器测试。"""

from __future__ import annotations

import pytest

from application.memory import MemoryService
from domain.memory import (
    MemoryListRequest,
    MemoryPage,
    MemoryRecord,
    MemorySearchRequest,
    MemoryWriteCommand,
)


class FakeMemoryPort:
    def __init__(self) -> None:
        self.commands: list[MemoryWriteCommand] = []

    async def initialize(self) -> None:
        return None

    async def search(self, request: MemorySearchRequest) -> list[MemoryRecord]:
        return [MemoryRecord("m-1", request.query, score=0.9)]

    async def list(self, request: MemoryListRequest) -> MemoryPage:
        return MemoryPage((MemoryRecord("m-1", "value"),), 1)

    async def get(self, memory_id: str) -> MemoryRecord | None:
        return MemoryRecord(memory_id, "value")

    async def revisions(self, memory_id: str) -> list[MemoryRecord]:
        return [MemoryRecord(memory_id, "value")]

    async def add(self, command: MemoryWriteCommand) -> str:
        self.commands.append(command)
        return "m-1"

    async def revise(self, *args, **kwargs) -> str:
        return "m-2"

    async def set_validity(self, *args, **kwargs) -> bool:
        return True

    async def delete(self, memory_id: str) -> None:
        return None

    async def flush_access_stats(self) -> int:
        return 1


@pytest.mark.asyncio
async def test_memory_service_delegates_typed_use_cases() -> None:
    port = FakeMemoryPort()
    service = MemoryService(port)

    assert (await service.search(MemorySearchRequest("hello")))[0].score == 0.9
    assert (await service.list(MemoryListRequest())).total == 1
    assert await service.add(MemoryWriteCommand("fact", {"session_id": "s-1"})) == "m-1"
    assert port.commands[0].metadata["session_id"] == "s-1"
    assert await service.revise("m-1", "new") == "m-2"


def test_memory_commands_reject_invalid_values() -> None:
    with pytest.raises(ValueError):
        MemorySearchRequest(" ")
    with pytest.raises(ValueError):
        MemoryListRequest(offset=-1)
    with pytest.raises(ValueError):
        MemoryWriteCommand("")

