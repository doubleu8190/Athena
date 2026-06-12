"""UnifiedMessage — channel-agnostic message dataclass.

All IM adapters convert their native message format into this unified
representation before dispatching to Core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass
class UnifiedMessage:
    """Normalized message from any IM channel.

    session_id is populated by Context Manager on first message
    (deterministic hash of user_id + channel + chat_id).
    Gateway layer passes an empty string — Context Manager fills it.
    """

    message_id: str                   # Channel-unique ID for dedup & reply positioning
    channel: Literal["telegram", "web", "wechat"]
    user_id: str
    session_id: str = ""              # Filled by Context Manager
    content: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    attachments: list[dict[str, Any]] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    chat_type: str | None = None      # "private" | "group" | "channel"
    reply_token: str | None = None    # For reply positioning:
    # WeChat: context_token (sent back on reply)
    # Telegram: original message.message_id (used as reply_to_message_id)
    # Web: null or original message_id for UI reply chain
