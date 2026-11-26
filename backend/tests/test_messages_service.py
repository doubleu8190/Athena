"""消息只读应用服务校验测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

import pytest

from app.message import InvalidMessageError, MessageService, SessionNotFoundError
from domain.message import (
    ConversationMessage,
    ConversationMessagePage,
    ConversationRole,
    MessageMetadata,
    MessageRepository,
    MessageStatus,
    TextPart,
)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


class RecordingMessageRepository(MessageRepository):
    """记录只读分页查询参数并返回预设结果的内存仓储。"""

    def __init__(self, page: ConversationMessagePage | None = None) -> None:
        self.page = page
        self.calls: list[tuple[str, str, int | None, int]] = []

    async def list_conversation_for_owner(
        self,
        session_id: str,
        owner_id: str,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> ConversationMessagePage | None:
        self.calls.append((session_id, owner_id, before_sequence, limit))
        return self.page


def _message(message_id: str, sequence: int) -> ConversationMessage:
    timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return ConversationMessage(
        id=message_id,
        session_id="session_1",
        role=ConversationRole.USER,
        status=MessageStatus.COMPLETED,
        sequence=sequence,
        parts=(TextPart("问题"),),
        reply_to_message_id=None,
        run_id="run_1",
        context_turn_id=message_id,
        client_message_id="client_1",
        created_at=timestamp,
        completed_at=timestamp,
        metadata=MessageMetadata(provider="test"),
    )


def test_list_conversation_returns_public_result_and_forwards_parameters() -> None:
    async def scenario() -> None:
        repository = RecordingMessageRepository(
            ConversationMessagePage(
                items=(_message("msg_1", 1),),
                next_before_sequence=None,
            )
        )
        service = MessageService(repository)

        result = await service.list_conversation(
            "session_1",
            viewer_id="user_1",
            before_sequence=5,
            limit=10,
        )

        assert repository.calls == [("session_1", "user_1", 5, 10)]
        assert result.next_before_sequence is None
        assert len(result.items) == 1
        assert result.items[0].id == "msg_1"
        assert result.items[0].run_id == "run_1"
        assert result.items[0].parts == (TextPart("问题"),)

    _run(scenario())


def test_list_conversation_raises_session_not_found_when_repository_returns_none() -> None:
    async def scenario() -> None:
        repository = RecordingMessageRepository(None)
        service = MessageService(repository)

        with pytest.raises(SessionNotFoundError):
            await service.list_conversation("session_missing", viewer_id="user_1")
        assert repository.calls == [("session_missing", "user_1", None, 20)]

    _run(scenario())


@pytest.mark.parametrize(
    ("session_id", "viewer_id", "before_sequence", "limit"),
    [
        ("", "user_1", None, 20),
        ("   ", "user_1", None, 20),
        ("s" * 65, "user_1", None, 20),
        ("session_1", "", None, 20),
        ("session_1", "   ", None, 20),
        ("session_1", "u" * 129, None, 20),
        ("session_1", "user_1", 0, 20),
        ("session_1", "user_1", -3, 20),
        ("session_1", "user_1", None, 0),
        ("session_1", "user_1", None, 101),
    ],
)
def test_list_conversation_rejects_invalid_identifiers_and_pagination(
    session_id: str,
    viewer_id: str,
    before_sequence: int | None,
    limit: int,
) -> None:
    async def scenario() -> None:
        repository = RecordingMessageRepository(
            ConversationMessagePage(items=(), next_before_sequence=None)
        )
        service = MessageService(repository)

        with pytest.raises(InvalidMessageError):
            await service.list_conversation(
                session_id,
                viewer_id=viewer_id,
                before_sequence=before_sequence,
                limit=limit,
            )
        assert repository.calls == []

    _run(scenario())
