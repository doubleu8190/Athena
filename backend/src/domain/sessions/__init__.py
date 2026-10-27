"""会话领域。"""

from .entities import Session
from domain.files.entities import AttachmentRef, AttachmentStatus

from .messages import Message, MessageRole, ToolCall
from .ports import MessageRepository, SessionRepository

__all__ = [
    "AttachmentRef",
    "AttachmentStatus",
    "Message",
    "MessageRepository",
    "MessageRole",
    "Session",
    "SessionRepository",
    "ToolCall",
]
