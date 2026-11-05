"""消息应用服务校验测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

import pytest

from app.message import InvalidMessageError, MessageService
from domain.message import (
    ConversationMessage,
    ConversationMessageDraft,
    ConversationMessagePage,
    ConversationRole,
    ExecutionMessage,
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    MessageRepository,
    MessageStatus,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


class RecordingMessageRepository(MessageRepository):
    """记录写入草稿并返回固定顺序的内存仓储。"""

    def __init__(self) -> None:
        self.conversation_drafts: list[ConversationMessageDraft] = []
        self.execution_drafts: list[ExecutionMessageDraft] = []

    async def append_conversation(
        self,
        draft: ConversationMessageDraft,
    ) -> ConversationMessage:
        self.conversation_drafts.append(draft)
        return ConversationMessage(
            id=draft.id,
            session_id=draft.session_id,
            role=draft.role,
            status=draft.status,
            sequence=len(self.conversation_drafts),
            parts=draft.parts,
            reply_to_message_id=draft.reply_to_message_id,
            run_id=draft.run_id,
            client_message_id=draft.client_message_id,
            created_at=draft.created_at,
            completed_at=draft.completed_at,
            metadata=draft.metadata,
        )

    async def append_execution(
        self,
        draft: ExecutionMessageDraft,
    ) -> ExecutionMessage:
        self.execution_drafts.append(draft)
        return ExecutionMessage(
            id=draft.id,
            session_id=draft.session_id,
            run_id=draft.run_id,
            thread_id=draft.thread_id,
            sequence=len(self.execution_drafts),
            role=draft.role,
            parts=draft.parts,
            tool_call_id=draft.tool_call_id,
            source_message_id=draft.source_message_id,
            created_at=draft.created_at,
            metadata=draft.metadata,
        )

    async def list_conversation_for_owner(
        self,
        session_id: str,
        owner_id: str,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> ConversationMessagePage | None:
        return ConversationMessagePage(items=(), next_before_sequence=None)


def _service() -> tuple[MessageService, RecordingMessageRepository]:
    repository = RecordingMessageRepository()
    return (
        MessageService(
            repository,
            id_factory=lambda: "msg_fixed",
            clock=lambda: datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        ),
        repository,
    )


@pytest.mark.parametrize(
    "parts",
    [
        (),
        (TextPart("   "),),
        (ToolResultPart("call_1", ToolResultStatus.SUCCESS, output="结果"),),
    ],
)
def test_conversation_rejects_empty_or_internal_parts(
    parts: tuple[TextPart | ToolResultPart, ...],
) -> None:
    async def scenario() -> None:
        service, repository = _service()
        with pytest.raises(InvalidMessageError):
            await service.append_user_message(
                "session_1",
                parts,
                owner_id="user_1",
                run_id="run_1",
                client_message_id="client_1",
            )
        assert repository.conversation_drafts == []

    _run(scenario())


def test_assistant_rejects_non_final_status_in_phase_one() -> None:
    async def scenario() -> None:
        service, _ = _service()
        with pytest.raises(InvalidMessageError):
            await service.append_assistant_message(
                "session_1",
                (TextPart("回答"),),
                owner_id="user_1",
                run_id="run_1",
                reply_to_message_id="msg_user",
                status=MessageStatus.STREAMING,
            )

    _run(scenario())


@pytest.mark.parametrize(
    "parts",
    [
        (
            ToolResultPart("call_1", ToolResultStatus.SUCCESS, output="一"),
            TextPart("额外内容"),
        ),
        (
            ToolCallPart("call_1", "search", {}),
            ToolCallPart("call_1", "fetch", {}),
        ),
    ],
)
def test_execution_rejects_invalid_tool_message_shapes(
    parts: tuple[TextPart | ToolCallPart | ToolResultPart, ...],
) -> None:
    async def scenario() -> None:
        service, repository = _service()
        role = (
            ExecutionRole.TOOL if isinstance(parts[0], ToolResultPart) else ExecutionRole.ASSISTANT
        )
        with pytest.raises(InvalidMessageError):
            await service.append_execution_message(
                "session_1",
                owner_id="user_1",
                run_id="run_1",
                thread_id="root:run_1",
                role=role,
                parts=parts,
            )
        assert repository.execution_drafts == []

    _run(scenario())


def test_tool_message_derives_envelope_call_id() -> None:
    async def scenario() -> None:
        service, repository = _service()
        await service.append_execution_message(
            "session_1",
            owner_id="user_1",
            run_id="run_1",
            thread_id="root:run_1",
            role=ExecutionRole.TOOL,
            parts=(
                ToolResultPart(
                    "call_1",
                    ToolResultStatus.SUCCESS,
                    output="结果",
                ),
            ),
        )
        assert repository.execution_drafts[0].tool_call_id == "call_1"

    _run(scenario())


def test_append_user_builds_completed_message_with_final_status() -> None:
    async def scenario() -> None:
        service, repository = _service()
        result = await service.append_user_message(
            "session_1",
            (TextPart("问题"),),
            owner_id="user_1",
            run_id="run_1",
            client_message_id="client_1",
            metadata=MessageMetadata(provider="test"),
        )

        draft = repository.conversation_drafts[0]
        assert result.role is ConversationRole.USER
        assert result.status is MessageStatus.COMPLETED
        assert draft.client_message_id == "client_1"
        assert draft.metadata == MessageMetadata(provider="test")

    _run(scenario())
