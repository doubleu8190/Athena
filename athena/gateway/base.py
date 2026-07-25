"""Base IM Adapter — abstract base class for all channel adapters.

Defines the contract that all adapters (Telegram, WeChat, Web) must satisfy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class AdapterState(StrEnum):
    DISCONNECTED = "disconnected"
    AUTHENTICATING = "authenticating"     # WeChat: scanning QR
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass
class AdapterInfo:
    """Status information exposed to the admin API / dashboard."""
    channel: str
    state: AdapterState
    last_heartbeat: datetime | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConfirmationRequest:
    """Standard confirmation request for user approval.

    Sent by Executor when a high-risk operation requires user confirmation.
    Gateway adapters translate this into channel-specific UI.
    """
    task_id: str
    risk_level: str          # "low" | "medium" | "high" | "critical"
    preview_text: str        # Human-readable summary of the operation
    cooling_off_seconds: int
    timeout_seconds: int
    nonce: str               # 64 random hex chars, replay protection
    channel: str
    user_id: str
    chat_id: str


class BaseIMAdapter(ABC):
    """Abstract base class for all IM channel adapters.

    Long-polling adapters (Telegram, WeChat) implement start_long_poll().
    All adapters implement send_message() and send_confirmation().
    """

    @property
    @abstractmethod
    def channel(self) -> str:
        """Channel identifier string, e.g. "telegram"."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Initialize the adapter. Called once at application startup.

        Long-polling adapters start a background asyncio task.
        Web adapter registers internal routes — no persistent connection needed.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Graceful shutdown. Cancel long-poll loops, close connections."""
        ...

    @abstractmethod
    async def send_message(
        self,
        user_id: str,
        chat_id: str,
        text: str,
        reply_token: str | None = None,
        inline_keyboard: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        """Send a message to this channel. Returns the platform-assigned message ID."""
        ...

    @abstractmethod
    async def send_confirmation(
        self,
        user_id: str,
        chat_id: str,
        confirmation: ConfirmationRequest,
    ) -> None:
        """Send a user confirmation request in the channel's native format."""
        ...

    @abstractmethod
    def get_info(self) -> AdapterInfo:
        """Return current adapter state for dashboard / health checks."""
        ...

    async def _dispatch_to_core(self, unified_msg) -> None:
        """Dispatch a UnifiedMessage to the Core processing pipeline.

        This callback is injected by GatewayManager during adapter registration.
        """
        if self._core_dispatch:
            await self._core_dispatch(unified_msg)

    # Injected by GatewayManager
    _core_dispatch: Any = None

