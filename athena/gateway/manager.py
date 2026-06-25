"""GatewayManager — adapter lifecycle coordination and message routing.

Manages all IM adapters:
- Instantiates enabled adapters based on user.yaml configuration
- Injects _core_dispatch callback into each adapter
- Coordinates start/stop at application startup/shutdown
- Routes outbound messages and confirmations to the correct adapter
- Exposes adapter status for admin API / dashboard
"""

from __future__ import annotations

from typing import Any

from athena.config import Config
from athena.gateway.base import AdapterInfo, BaseIMAdapter, ConfirmationRequest
from athena.logging_config import get_logger

logger = get_logger(__name__)


class GatewayManager:
    """Coordinates all IM channel adapters.

    Responsibilities:
    - Lifecycle management (start/stop all adapters)
    - Message routing: inbound → Core dispatch, outbound → correct adapter
    - Confirmation routing: nonce → adapter → user
    - Status aggregation for admin API
    """

    def __init__(self, config: Config):
        self.config = config
        self._adapters: dict[str, BaseIMAdapter] = {}
        self._pending_confirmations: dict[str, ConfirmationRequest] = {}
        # Confirmation futures: nonce → Future that resolves when user responds
        self._confirmation_futures: dict[str, Any] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize and start all enabled channel adapters."""
        user = self.config.user

        # Instantiate adapters based on config
        if user.is_channel_enabled("web"):
            from athena.gateway.web import WebAdapter
            adapter = WebAdapter(self.config)
            adapter._core_dispatch = self._dispatch_to_core
            self._adapters["web"] = adapter
            logger.info("gateway_adapter_registered", channel="web")

        if user.is_channel_enabled("telegram"):
            from athena.gateway.telegram import TelegramAdapter
            adapter = TelegramAdapter(self.config)
            adapter._core_dispatch = self._dispatch_to_core
            self._adapters["telegram"] = adapter
            logger.info("gateway_adapter_registered", channel="telegram")

        if user.is_channel_enabled("wechat"):
            from athena.gateway.wechat import WeChatAdapter
            adapter = WeChatAdapter(self.config)
            adapter._core_dispatch = self._dispatch_to_core
            self._adapters["wechat"] = adapter
            logger.info("gateway_adapter_registered", channel="wechat")

        # Start all adapters
        for channel, adapter in self._adapters.items():
            try:
                await adapter.start()
                logger.info("gateway_adapter_started", channel=channel)
            except Exception as e:
                logger.error(
                    "gateway_adapter_start_failed",
                    channel=channel,
                    error=str(e),
                )

    async def stop(self) -> None:
        """Gracefully stop all adapters."""
        for channel, adapter in self._adapters.items():
            try:
                await adapter.stop()
                logger.info("gateway_adapter_stopped", channel=channel)
            except Exception as e:
                logger.error(
                    "gateway_adapter_stop_failed",
                    channel=channel,
                    error=str(e),
                )
        self._adapters.clear()

    # ── Dispatch ─────────────────────────────────────────────────────

    async def _dispatch_to_core(self, unified_msg) -> None:
        """Route an inbound message to the Core processing pipeline.

        Called by individual adapters when they receive a message.
        This is the entry point into the Context → Planner → Executor pipeline.
        """
        logger.info(
            "message_dispatched_to_core",
            channel=unified_msg.channel,
            user_id=unified_msg.user_id,
            message_id=unified_msg.message_id,
        )
        # In production, this would be imported from main or injected
        # The actual Core processing happens here
        # For now, log and acknowledge
        from athena.core.context import ContextManager

        context_mgr = ContextManager(self.config)

        # Get or create session
        session_ctx = await context_mgr.get_or_create_session(
            unified_msg.user_id,
            unified_msg.channel,
            unified_msg.chat_id or unified_msg.user_id,
        )

        # The full pipeline (Planner → Executor) is triggered from here
        # This is a simplified dispatch; full pipeline integration
        # happens when the API layer is connected

    # ── Adapter access ────────────────────────────────────────────────

    def get_adapter(self, channel: str) -> BaseIMAdapter | None:
        """Get an adapter by channel name."""
        return self._adapters.get(channel)

    def get_all_adapters(self) -> dict[str, BaseIMAdapter]:
        """Get all registered adapters."""
        return dict(self._adapters)

    def get_all_status(self) -> list[AdapterInfo]:
        """Aggregate status from all adapters for admin API."""
        return [adapter.get_info() for adapter in self._adapters.values()]

    async def send_message(
        self,
        channel: str,
        user_id: str,
        chat_id: str,
        text: str,
        **kwargs,
    ) -> str | None:
        """Send a message through a specific channel adapter."""
        adapter = self._adapters.get(channel)
        if not adapter:
            logger.warning("gateway_channel_not_found", channel=channel)
            return None
        return await adapter.send_message(user_id, chat_id, text, **kwargs)

    async def send_confirmation(
        self,
        channel: str,
        user_id: str,
        chat_id: str,
        confirmation: ConfirmationRequest,
    ) -> None:
        """Send a confirmation request through a specific channel adapter."""
        adapter = self._adapters.get(channel)
        if not adapter:
            logger.warning("gateway_channel_not_found", channel=channel)
            return

        # Store for timeout tracking
        self._pending_confirmations[confirmation.nonce] = confirmation

        await adapter.send_confirmation(user_id, chat_id, confirmation)
