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


# ── Singleton ───────────────────────────────────────────────────────────────

_gateway_manager: GatewayManager | None = None


def set_gateway_manager(manager: GatewayManager) -> None:
    """Set the process-wide singleton GatewayManager instance.

    Called once during application startup (lifespan). Must be called
    before any call to get_gateway_manager().
    """
    global _gateway_manager
    _gateway_manager = manager
    logger.info("gateway_manager_singleton_set")


def get_gateway_manager() -> GatewayManager:
    """Return the process-wide singleton GatewayManager instance.

    Raises RuntimeError if not yet initialized. The lifespan must call
    set_gateway_manager() during startup before any route handler accesses this.
    """
    if _gateway_manager is None:
        raise RuntimeError(
            "GatewayManager not initialized — call set_gateway_manager() during lifespan startup"
        )
    return _gateway_manager


class GatewayManager:
    """Coordinates all IM channel adapters.

    Responsibilities:
    - Lifecycle management (start/stop all adapters)
    - Message routing: inbound → Core dispatch, outbound → correct adapter
    - Confirmation routing: nonce → adapter → user
    - Status aggregation for admin API
    """

    def __init__(self, config: Config) -> None:
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
        This is the entry point into the LangGraph agent graph pipeline.

        Args:
            unified_msg: A UnifiedMessage instance containing the normalized message
                from any IM channel (Telegram, WeChat, etc.)

        The pipeline:
            1. Get or create session via ContextManager
            2. Build and execute LangGraph agent graph
            3. Handle interrupts for user confirmation
            4. Stream results back to the originating adapter
        """
        logger.info(
            "message_dispatched_to_core",
            channel=unified_msg.channel,
            user_id=unified_msg.user_id,
            message_id=unified_msg.message_id,
        )

        checkpointer = None
        try:
            # ── Setup: Context and Session ────────────────────────────
            from langchain_core.messages import HumanMessage
            from langchain_core.runnables import RunnableConfig
            from langgraph.graph.state import CompiledStateGraph

            from athena.core.context import ContextManager
            from athena.core.graph import build_agent_graph
            from athena.core.graph.agent_graph import create_checkpointer

            context_mgr = ContextManager()

            # Get or create session
            session = await context_mgr.get_or_create_session(
                unified_msg.user_id,
                unified_msg.channel,
                unified_msg.chat_id or unified_msg.user_id,
            )

            # ── Setup: Checkpointer ────────────────────────────────────
            checkpointer = await create_checkpointer(self.config.sqlite_db_path)

            # ── Setup: Agent Graph ────────────────────────────────────
            graph: CompiledStateGraph = build_agent_graph(
                checkpointer=checkpointer,
            )

            # ── Initial State ─────────────────────────────────────────
            # Checkpointer restores prior messages/history;
            # we only seed the dynamic system context + the new user message.
            initial_state = {
                "messages": [HumanMessage(content=unified_msg.content)],
            }

            # ── Graph Config ──────────────────────────────────────────
            graph_config: RunnableConfig = {
                "configurable": {
                    "thread_id": session.session_id,
                }
            }

            # ── Stream Graph Execution ────────────────────────────────
            # Use stream_mode="updates" to get per-node updates
            collected_response = []
            gateway_decisions: dict[str, str] = {}

            while True:
                input_state: Any = initial_state
                if gateway_decisions:
                    input_state = {**initial_state, "_confirmation_results": gateway_decisions}

                loop_done = False
                async for event in graph.astream(
                    input_state,
                    config=graph_config,
                    stream_mode="updates",
                ):
                    # Handle awaiting confirmation (state-based flow)
                    for node_name, node_output in event.items():
                        if node_name == "confirm" and node_output.get("_awaiting_confirmation"):
                            pending_tools = node_output["_awaiting_confirmation"]
                            await self._handle_confirmation_request(
                                unified_msg.channel,
                                unified_msg.user_id,
                                unified_msg.chat_id or unified_msg.user_id,
                                pending_tools,
                                session.session_id,
                            )
                            loop_done = True
                            break

                    if loop_done:
                        break

                    # Convert LangGraph events to adapter messages
                    for node_name, node_output in event.items():
                        if node_name == "agent":
                            # ── Agent node output ────────────────────────
                            messages = node_output.get("messages", [])
                            status = node_output.get("status", "")

                            for msg in messages:
                                if hasattr(msg, "content") and msg.content:
                                    content = (
                                        msg.content
                                        if isinstance(msg.content, str)
                                        else str(msg.content)
                                    )
                                    if content.strip():
                                        collected_response.append(content)

                            if status == "completed":
                                # Send the final response
                                if collected_response:
                                    await self.send_message(
                                        channel=unified_msg.channel,
                                        user_id=unified_msg.user_id,
                                        chat_id=unified_msg.chat_id or unified_msg.user_id,
                                        text="\n".join(collected_response),
                                        reply_token=unified_msg.reply_token,
                                    )
                                loop_done = True
                                break

                        elif node_name == "tools":
                            # ── Tools node output ────────────────────────
                            # Tool results are processed internally by the graph
                            # but we can log them for debugging
                            t_messages = node_output.get("messages", [])
                            for msg in t_messages:
                                if hasattr(msg, "content"):
                                    logger.info(
                                        "tool_execution_result",
                                        content=str(msg.content)[:200],
                                    )

                if not loop_done:
                    break

        except Exception as e:
            logger.error(
                "dispatch_to_core_error",
                channel=unified_msg.channel,
                user_id=unified_msg.user_id,
                error=str(e),
            )
            # Send error message back to user
            try:
                await self.send_message(
                    channel=unified_msg.channel,
                    user_id=unified_msg.user_id,
                    chat_id=unified_msg.chat_id or unified_msg.user_id,
                    text=f"处理消息时发生错误: {str(e)}",
                    reply_token=unified_msg.reply_token,
                )
            except Exception as send_err:
                logger.error(
                    "dispatch_error_notification_failed",
                    error=str(send_err),
                )
        finally:
            if checkpointer is not None:
                try:
                    await checkpointer.conn.close()
                except Exception as close_err:
                    logger.error(
                        "checkpointer_close_error",
                        error=str(close_err),
                    )

    async def _handle_confirmation_request(
        self,
        channel: str,
        user_id: str,
        chat_id: str,
        pending_tools: list[dict[str, Any]],
        session_id: str,
    ) -> None:
        """Handle confirmation requests using state-based flow.

        Sends confirmation requests via the adapter and stores pending
        approvals.  The graph execution loop will be resumed externally
        via ``handle_confirmation_response`` when the user responds.

        Args:
            channel: The IM channel (telegram, wechat, etc.)
            user_id: The user ID
            chat_id: The chat ID
            pending_tools: List of tool dicts needing user approval
            session_id: The session ID for resuming
        """
        from athena.gateway.confirmation import ConfirmationManager

        confirm_mgr = ConfirmationManager()

        for tc in pending_tools:
            tool_call_id = tc.get("id", "")
            tool_name = tc.get("name", "")
            risk_level = tc.get("_harness_risk_level", "medium")
            cooling_off = tc.get("_harness_cooling_off", 0)
            reason = tc.get("_harness_reason", "")

            # Create confirmation request
            confirmation = await confirm_mgr.create_confirmation(
                task_id=tool_call_id,
                risk_level=risk_level,
                preview_text=reason or f"Operation requires confirmation: {tool_name}",
                cooling_off_seconds=cooling_off,
                timeout_seconds=300,
                channel=channel,
                user_id=user_id,
                chat_id=chat_id,
            )

            # Send confirmation via adapter
            await self.send_confirmation(
                channel=channel,
                user_id=user_id,
                chat_id=chat_id,
                confirmation=confirmation,
            )

            logger.info(
                "confirmation_sent",
                channel=channel,
                session_id=session_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                risk_level=risk_level,
            )

    async def handle_confirmation_response(
        self,
        channel: str,
        user_id: str,
        chat_id: str,
        session_id: str,
        approved: bool,
    ) -> None:
        """Handle user's confirmation response and resume graph execution.

        Called by adapters when a user responds to a confirmation request.
        Resumes the LangGraph execution by injecting the decision via state
        (``_confirmation_results``) — no ``Command(resume)`` is used.

        Args:
            channel: The IM channel (telegram, wechat, etc.)
            user_id: The user ID
            chat_id: The chat ID
            session_id: The session ID to resume
            approved: Whether the user approved the operation
        """
        from langchain_core.runnables import RunnableConfig

        from athena.core.graph import build_agent_graph
        from athena.core.graph.agent_graph import create_checkpointer

        checkpointer = None
        try:
            logger.info(
                "confirmation_response_received",
                channel=channel,
                session_id=session_id,
                approved=approved,
            )

            # ── Setup ────────────────────────────────────────────────────
            checkpointer = await create_checkpointer(self.config.sqlite_db_path)

            # Build graph — topology must match the original
            graph = build_agent_graph(
                checkpointer=checkpointer,
            )

            graph_config: RunnableConfig = {
                "configurable": {
                    "thread_id": session_id,
                }
            }

            # ── Resume Execution via state injection ────────────────────
            decision = "approved" if approved else "rejected"
            collected_response = []
            resume_state: dict[str, Any] = {
                "_confirmation_results": {"_pending": decision},
            }

            async for event in graph.astream(
                resume_state,
                config=graph_config,
                stream_mode="updates",
            ):
                # Handle nested confirmation (state-based)
                for node_name, node_output in event.items():
                    if node_name == "confirm" and node_output.get("_awaiting_confirmation"):
                        pending_tools = node_output["_awaiting_confirmation"]
                        await self._handle_confirmation_request(
                            channel,
                            user_id,
                            chat_id,
                            pending_tools,
                            session_id,
                        )
                        return  # End here; user must confirm via another response

                # Convert LangGraph events to adapter messages
                for node_name, node_output in event.items():
                    if node_name == "agent":
                        messages = node_output.get("messages", [])
                        status = node_output.get("status", "")

                        for msg in messages:
                            if hasattr(msg, "content") and msg.content:
                                content = (
                                    msg.content
                                    if isinstance(msg.content, str)
                                    else str(msg.content)
                                )
                                if content.strip():
                                    collected_response.append(content)

                        if status == "completed":
                            # Send the final response
                            if collected_response:
                                await self.send_message(
                                    channel=channel,
                                    user_id=user_id,
                                    chat_id=chat_id,
                                    text="\n".join(collected_response),
                                )
                            return

                    elif node_name == "tools":
                        # Log tool execution results for debugging
                        t_messages = node_output.get("messages", [])
                        for msg in t_messages:
                            if hasattr(msg, "content"):
                                logger.info(
                                    "tool_execution_result_on_resume",
                                    content=str(msg.content)[:200],
                                )

        except Exception as e:
            logger.error(
                "confirmation_resume_error",
                channel=channel,
                session_id=session_id,
                error=str(e),
            )
            try:
                await self.send_message(
                    channel=channel,
                    user_id=user_id,
                    chat_id=chat_id,
                    text=f"恢复执行时发生错误: {str(e)}",
                )
            except Exception as send_err:
                logger.error(
                    "resume_error_notification_failed",
                    error=str(send_err),
                )
        finally:
            if checkpointer is not None:
                try:
                    await checkpointer.conn.close()
                except Exception as close_err:
                    logger.error(
                        "checkpointer_close_error_on_resume",
                        error=str(close_err),
                    )

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
        **kwargs: Any,  # noqa: ANN401
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
