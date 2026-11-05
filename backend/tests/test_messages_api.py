"""消息只读 HTTP API 测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from auth_support import AuthTestClient, make_auth_service
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.auth.service import AuthService
from app.message import MessageService
from domain.message import (
    ConversationMessage,
    ConversationMessageDraft,
    ConversationMessagePage,
    ConversationRole,
    ExecutionMessage,
    ExecutionMessageDraft,
    MessageMetadata,
    MessageStatus,
    TextPart,
)
from main import create_app


class QueryMessageRepository:
    """为 HTTP 测试提供固定会话消息页。"""

    def __init__(self, messages: tuple[ConversationMessage, ...] = ()) -> None:
        self.messages = messages
        self.calls: list[tuple[str, str, int | None, int]] = []

    async def append_conversation(
        self,
        draft: ConversationMessageDraft,
    ) -> ConversationMessage:
        raise AssertionError("read API must not append conversation messages")

    async def append_execution(
        self,
        draft: ExecutionMessageDraft,
    ) -> ExecutionMessage:
        raise AssertionError("read API must not append execution messages")

    async def list_conversation_for_owner(
        self,
        session_id: str,
        owner_id: str,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> ConversationMessagePage | None:
        self.calls.append((session_id, owner_id, before_sequence, limit))
        if session_id != "session_1" or owner_id != "user_1":
            return None
        rows = [
            item
            for item in self.messages
            if before_sequence is None or item.sequence < before_sequence
        ]
        rows.sort(key=lambda item: item.sequence)
        return ConversationMessagePage(
            items=tuple(rows[-limit:]),
            next_before_sequence=None,
        )


class FailingMessageRepository(QueryMessageRepository):
    """模拟消息查询时数据库不可用。"""

    async def list_conversation_for_owner(
        self,
        session_id: str,
        owner_id: str,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> ConversationMessagePage | None:
        raise OperationalError(
            "SELECT conversation_messages",
            {},
            RuntimeError("database password must not leak"),
        )


class TransportFailingMessageRepository(QueryMessageRepository):
    """模拟驱动连接阶段直接抛出 OSError。"""

    async def list_conversation_for_owner(
        self,
        session_id: str,
        owner_id: str,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> ConversationMessagePage | None:
        raise ConnectionRefusedError("database host and password must not leak")


def _message(
    message_id: str,
    sequence: int,
    *,
    role: ConversationRole,
    text: str,
) -> ConversationMessage:
    timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return ConversationMessage(
        id=message_id,
        session_id="session_1",
        role=role,
        status=MessageStatus.COMPLETED,
        sequence=sequence,
        parts=(TextPart(text),),
        reply_to_message_id="msg_user" if role is ConversationRole.ASSISTANT else None,
        run_id="run_1",
        client_message_id="client_1" if role is ConversationRole.USER else None,
        created_at=timestamp,
        completed_at=timestamp,
        metadata=MessageMetadata(),
    )


def _client(
    repository: QueryMessageRepository,
    *,
    auth_service: AuthService | None = None,
    email: str = "user@example.com",
) -> AuthTestClient:
    service = MessageService(repository)
    return AuthTestClient(
        create_app(
            message_service=service,
            auth_service=auth_service or make_auth_service(),
        ),
        email=email,
    )


def test_list_messages_returns_public_parts_and_query_parameters() -> None:
    repository = QueryMessageRepository(
        (
            _message(
                "msg_user",
                1,
                role=ConversationRole.USER,
                text="问题",
            ),
            _message(
                "msg_assistant",
                2,
                role=ConversationRole.ASSISTANT,
                text="回答",
            ),
        )
    )

    with _client(repository) as client:
        response = client.get(
            "/api/v1/sessions/session_1/messages",
            params={"before_sequence": 3, "limit": 1},
        )

    assert response.status_code == 200
    assert repository.calls == [("session_1", "user_1", 3, 1)]
    body = response.json()
    assert body["next_before_sequence"] is None
    assert body["items"][0]["id"] == "msg_assistant"
    assert body["items"][0]["parts"] == [{"type": "text", "text": "回答"}]
    assert "metadata" not in body["items"][0]


def test_missing_or_foreign_session_returns_generic_404() -> None:
    repository = QueryMessageRepository()
    auth_service = make_auth_service()
    with _client(
        repository,
        auth_service=auth_service,
        email="owner@example.com",
    ) as owner:
        owner_response = owner.get("/api/v1/sessions/session_1/messages")
    assert owner_response.status_code == 200
    repository.calls.clear()

    with _client(
        repository,
        auth_service=auth_service,
        email="another@example.com",
    ) as client:
        response = client.get("/api/v1/sessions/session_1/messages")
    assert response.status_code == 404
    assert response.json() == {"detail": "session_not_found"}


def test_message_route_returns_401_without_authentication() -> None:
    service = MessageService(
        QueryMessageRepository(),
    )
    with TestClient(create_app(message_service=service)) as client:
        response = client.get("/api/v1/sessions/session_1/messages")
    assert response.status_code == 401
    assert response.json() == {"detail": "not_authenticated"}


@pytest.mark.parametrize(
    "params",
    [
        {"before_sequence": 0},
        {"limit": 0},
        {"limit": 101},
    ],
)
def test_invalid_pagination_is_rejected(params: dict[str, int]) -> None:
    with _client(QueryMessageRepository()) as client:
        response = client.get(
            "/api/v1/sessions/session_1/messages",
            params=params,
        )
    assert response.status_code == 422


def test_database_failure_returns_503_without_leaking_details() -> None:
    with _client(FailingMessageRepository()) as client:
        response = client.get("/api/v1/sessions/session_1/messages")

    assert response.status_code == 503
    assert response.json() == {"detail": "database_unavailable"}
    assert "password" not in response.text
    assert "SELECT conversation_messages" not in response.text


def test_database_transport_failure_returns_503_without_leaking_details() -> None:
    with _client(TransportFailingMessageRepository()) as client:
        response = client.get("/api/v1/sessions/session_1/messages")

    assert response.status_code == 503
    assert response.json() == {"detail": "database_unavailable"}
    assert "password" not in response.text
    assert "database host" not in response.text
