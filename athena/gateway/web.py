"""Web Console adapter — internal API + SSE streaming.

Provides the web channel for the management console's command interface.
Messages arrive via POST /api/v1/im/web/message and responses are
streamed via Server-Sent Events (SSE).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from athena.config import Config
from athena.gateway.base import (
    AdapterInfo,
    AdapterState,
    BaseIMAdapter,
    ConfirmationRequest,
)
from athena.logging_config import get_logger

logger = get_logger(__name__)


class WebAdapter(BaseIMAdapter):
    """Web console adapter for the built-in management interface.

    Uses internal API calls (no external protocol).
    SSE is used for streaming execution progress and final replies.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._state = AdapterState.CONNECTED
        self._last_heartbeat = datetime.now(timezone.utc)
        self._error: str | None = None
        # SSE queues: task_id → asyncio.Queue for streaming events
        self._sse_queues: dict[str, asyncio.Queue] = {}
        # Confirmation futures: nonce → Future
        self._confirmation_futures: dict[str, asyncio.Future] = {}

    @property
    def channel(self) -> str:
        return "web"

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Web adapter has no persistent connection to manage."""
        self._state = AdapterState.CONNECTED
        logger.info("web_adapter_started")

    async def stop(self) -> None:
        """Clean up any pending SSE queues."""
        for queue in self._sse_queues.values():
            await queue.put(None)  # Sentinel to close streams
        self._sse_queues.clear()
        self._confirmation_futures.clear()
        self._state = AdapterState.DISCONNECTED
        logger.info("web_adapter_stopped")

    # ── Message sending ───────────────────────────────────────────────

    async def send_message(
        self,
        user_id: str,
        chat_id: str,
        text: str,
        reply_token: str | None = None,
        inline_keyboard: dict | None = None,
        attachments: list[dict] | None = None,
    ) -> str:
        """Send a message via SSE event stream."""
        msg_id = f"web_{datetime.now(timezone.utc).timestamp()}"
        # Messages are delivered via SSE, not a direct return
        logger.info("web_message_queued", text_preview=text[:100])
        return msg_id

    async def send_confirmation(
        self,
        user_id: str,
        chat_id: str,
        confirmation: ConfirmationRequest,
    ) -> None:
        """Send confirmation via SSE confirm_required event."""
        # Create a future for the confirmation response
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._confirmation_futures[confirmation.nonce] = future

        logger.info(
            "web_confirmation_sent",
            task_id=confirmation.task_id,
            step=confirmation.step,
        )
        # The SSE event is pushed by the API layer

    # ── SSE event streaming ───────────────────────────────────────────

    def get_sse_queue(self, task_id: str) -> asyncio.Queue:
        """Get or create an SSE event queue for a task."""
        if task_id not in self._sse_queues:
            self._sse_queues[task_id] = asyncio.Queue()
        return self._sse_queues[task_id]

    async def push_sse_event(self, task_id: str, event_type: str, data: dict) -> None:
        """Push an SSE event to the client subscribed to this task."""
        queue = self._sse_queues.get(task_id)
        if queue:
            await queue.put({"event": event_type, "data": data})

    # ── Confirmation handling ─────────────────────────────────────────

    async def resolve_confirmation(self, nonce: str, approved: bool) -> bool:
        """Resolve a pending confirmation future. Called from the API."""
        future = self._confirmation_futures.pop(nonce, None)
        if future and not future.done():
            future.set_result(approved)
            return True
        return False

    # ── Status ────────────────────────────────────────────────────────

    def get_info(self) -> AdapterInfo:
        return AdapterInfo(
            channel="web",
            state=self._state,
            last_heartbeat=self._last_heartbeat,
            error=self._error,
            metadata={},
        )
