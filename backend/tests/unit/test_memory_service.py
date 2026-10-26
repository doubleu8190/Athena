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

    async def list_memories(self, request: MemoryListRequest) -> MemoryPage:
        return MemoryPage((MemoryRecord("m-1", "value"),), 1)

    async def get(self, memory_id: str) -> MemoryRecord | None:
        return MemoryRecord(memory_id, "value")

    async def revisions(self, memory_id: str) -> list[MemoryRecord]:
        return [MemoryRecord(memory_id, "value")]

    async def related_entries(self, memory_id: str):
        return []

    async def create_memory(self, command: MemoryWriteCommand) -> str:
        self.commands.append(command)
        return "m-1"

    async def revise(self, *args, **kwargs) -> str:
        return "m-2"

    async def update_validity(self, *args, **kwargs) -> bool:
        return True

    async def delete(self, memory_id: str) -> bool:
        return True

    async def flush_access_stats(self) -> int:
        return 1


@pytest.mark.asyncio
async def test_memory_service_delegates_typed_use_cases() -> None:
    port = FakeMemoryPort()
    service = MemoryService(port)

    assert (await service.search(MemorySearchRequest("hello")))[0].score == 0.9
    assert (await service.list_memories(MemoryListRequest())).total == 1
    assert await service.create_memory(MemoryWriteCommand("fact", {"session_id": "s-1"})) == "m-1"
    assert port.commands[0].metadata["session_id"] == "s-1"
    assert await service.revise("m-1", "new") == "m-2"


def test_memory_commands_reject_invalid_values() -> None:
    with pytest.raises(ValueError):
        MemorySearchRequest(" ")
    with pytest.raises(ValueError):
        MemoryListRequest(offset=-1)
    with pytest.raises(ValueError):
        MemoryWriteCommand("")

@pytest.mark.asyncio
async def test_memory_metadata_survives_repository_to_http_boundary() -> None:
    """验证数据库时间和统计经过服务、HTTP 转换后保持不变，无业务异常。"""
    from infrastructure.persistence.postgres.models import MemoryModel
    from infrastructure.persistence.postgres.repositories import PostgresMemoryRepository
    from application.memory.service import _to_response
    from interfaces.http.controllers.query_controller import _memory_response

    row = MemoryModel(
        id="m-1", content="fact", metadata_json="{}", source_kind="extraction",
        status="active", created_at="2026-10-01T00:00:00+00:00",
        access_count=3, last_accessed_at="2026-10-20T00:00:00+00:00",
        last_observed_at="2026-10-01T00:00:00+00:00", expires_at=None,
        valid_until="2026-11-01T00:00:00+00:00", validity_status="valid",
    )
    result = _memory_response(_to_response(PostgresMemoryRepository._domain(row)))
    assert result["created_at"] == row.created_at
    assert result["access_count"] == 3
    assert result["last_accessed_at"] == row.last_accessed_at
    assert result["last_observed_at"] == row.last_observed_at
    assert result["expires_at"] is None
    assert result["valid_until"] == row.valid_until
