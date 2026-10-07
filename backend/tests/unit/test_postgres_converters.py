"""Session/Message PostgreSQL 转换测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.src.domain.sessions import (
    AttachmentRef,
    AttachmentStatus,
    Message,
    MessageRole,
    Session,
    SessionStatus,
    ToolCall,
)
from backend.src.infrastructure.persistence.postgres.models import (
    MessageModel,
    SessionModel,
)
from backend.src.infrastructure.persistence.postgres.repositories.converters import (
    message_domain_to_model,
    message_model_to_domain,
    session_domain_to_model,
    session_model_to_domain,
)


def test_session_converter_preserves_compression_fields() -> None:
    """Session ORM 转换不能丢失压缩游标。"""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    domain = Session(
        id="session-1",
        title="Title",
        status=SessionStatus.IDLE,
        run_id=None,
        created_at=now,
        updated_at=now,
        compression_summary="summary",
        last_compressed_message_id="message-1",
        last_summarized_message_id="message-2",
    )

    restored = session_model_to_domain(session_domain_to_model(domain))

    assert restored == domain


def test_message_converter_round_trips_tool_calls_and_references() -> None:
    """Message ORM 转换保留工具调用和附件引用。"""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    domain = Message(
        id="message-1",
        session_id="session-1",
        role=MessageRole.ASSISTANT,
        content="done",
        timestamp=now,
        tool_calls=(ToolCall(id="call-1", name="search", args={"q": "x"}),),
        attachments=(
            AttachmentRef(
                id="file-1",
                filename="a.txt",
                mime_type="text/plain",
                size_bytes=1,
                status=AttachmentStatus.READY,
            ),
        ),
    )

    model = message_domain_to_model(domain)
    restored = message_model_to_domain(model, attachments=domain.attachments)

    assert restored == domain


def test_message_converter_ignores_malformed_tool_call_json() -> None:
    """历史脏 JSON 不应阻止消息查询。"""
    model = MessageModel(
        id="message-1",
        session_id="session-1",
        role="assistant",
        content="done",
        tool_calls_json='{"unexpected": true}',
        timestamp="2026-01-01T00:00:00+00:00",
    )

    restored = message_model_to_domain(model)

    assert restored.tool_calls == ()
