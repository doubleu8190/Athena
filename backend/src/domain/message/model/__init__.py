"""消息领域对象与异常。"""

from domain.message.model.entity import (
    ConversationMessage,
    ConversationMessageDraft,
    ConversationMessagePage,
    ConversationRole,
    ExecutionMessage,
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    MessageStatus,
)
from domain.message.model.errors import (
    InvalidMessageError,
    InvalidMessageReferenceError,
    MessageConflictError,
    SessionNotFoundError,
)
from domain.message.model.part import (
    FileRefPart,
    MessagePart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)

__all__ = [
    "ConversationMessage",
    "ConversationMessageDraft",
    "ConversationMessagePage",
    "ConversationRole",
    "ExecutionMessage",
    "ExecutionMessageDraft",
    "ExecutionRole",
    "FileRefPart",
    "InvalidMessageError",
    "InvalidMessageReferenceError",
    "MessageConflictError",
    "MessageMetadata",
    "MessagePart",
    "MessageStatus",
    "SessionNotFoundError",
    "TextPart",
    "ToolCallPart",
    "ToolResultPart",
    "ToolResultStatus",
]
