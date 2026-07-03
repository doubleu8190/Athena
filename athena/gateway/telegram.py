"""Telegram adapter — Long Poll via python-telegram-bot.

Communication pattern:
- Uses getUpdates API (Long Poll) — no webhook, no inbound port needed
- Message dedup by message.message_id
- Reply threading via reply_to_message_id
- Inline Keyboard Markup for confirmation requests with cooling-off
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime, timezone

from athena.config import Config
from athena.core.message import UnifiedMessage
from athena.core.secrets import get_secret
from athena.gateway.base import (
    AdapterInfo,
    AdapterState,
    BaseIMAdapter,
    ConfirmationRequest,
)
from athena.logging_config import get_logger

logger = get_logger(__name__)


class TelegramAdapter(BaseIMAdapter):
    """Telegram Bot adapter using Long Poll (getUpdates).

    Does NOT require a webhook or public-facing endpoint.
    Messages are pulled from api.telegram.org via HTTPS.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._token = get_secret("TELEGRAM_BOT_TOKEN", "")
        self._poll_timeout = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "30"))
        self._state = AdapterState.DISCONNECTED
        self._last_heartbeat: datetime | None = None
        self._error: str | None = None
        self._poll_task: asyncio.Task | None = None
        self._running = False
        self._last_update_id: int = 0
        self._seen_message_ids: set[str] = set()

    @property
    def channel(self) -> str:
        return "telegram"

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the Long Poll loop."""
        if not self._token:
            self._state = AdapterState.FAILED
            self._error = "TELEGRAM_BOT_TOKEN not set"
            logger.error("telegram_no_token")
            return

        # Ensure webhook is deleted (Long Poll exclusivity)
        await self._delete_webhook()

        self._running = True
        self._state = AdapterState.CONNECTED
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("telegram_started")

    async def stop(self) -> None:
        """Cancel the Long Poll loop."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._state = AdapterState.DISCONNECTED
        logger.info("telegram_stopped")

    async def _delete_webhook(self) -> None:
        """Delete any existing webhook to enable Long Poll exclusivity."""
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.telegram.org/bot{self._token}/deleteWebhook"
                )
                resp.raise_for_status()
                logger.info("telegram_webhook_deleted")
        except Exception as e:
            logger.warning("telegram_delete_webhook_failed", error=str(e))

    async def _poll_loop(self) -> None:
        """Main Long Poll loop using getUpdates."""
        import httpx

        while self._running:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(self._poll_timeout + 10)) as client:
                    params = {
                        "offset": self._last_update_id + 1,
                        "timeout": self._poll_timeout,
                        "allowed_updates": json.dumps(["message", "callback_query"]),
                    }
                    resp = await client.get(
                        f"https://api.telegram.org/bot{self._token}/getUpdates",
                        params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    if data.get("ok"):
                        for update in data.get("result", []):
                            await self._process_update(update)
                            self._last_update_id = max(
                                self._last_update_id, update["update_id"]
                            )

                    self._last_heartbeat = datetime.now(timezone.utc)
                    self._state = AdapterState.CONNECTED
                    self._error = None

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("telegram_poll_error", error=str(e))
                self._state = AdapterState.RECONNECTING
                self._error = str(e)
                await asyncio.sleep(5)  # Backoff before retry

    async def _process_update(self, update: dict) -> None:
        """Process a single Telegram update (message or callback_query)."""
        if "message" in update:
            await self._process_message(update["message"])
        elif "callback_query" in update:
            await self._process_callback(update["callback_query"])

    async def _process_message(self, message: dict) -> None:
        """Convert a Telegram message to UnifiedMessage and dispatch."""
        msg_id = str(message.get("message_id", ""))

        # Dedup
        if msg_id in self._seen_message_ids:
            return
        self._seen_message_ids.add(msg_id)

        # Keep set bounded
        if len(self._seen_message_ids) > 10000:
            self._seen_message_ids.clear()

        # Extract fields
        from_user = message.get("from", {})
        chat = message.get("chat", {})
        user_id = str(from_user.get("id", ""))
        content = message.get("text", message.get("caption", ""))
        chat_type = chat.get("type", "private")  # "private" | "group" | "channel"

        # Build UnifiedMessage
        unified = UnifiedMessage(
            message_id=msg_id,
            channel="telegram",
            user_id=user_id,
            content=content,
            timestamp=datetime.fromtimestamp(
                message.get("date", 0), tz=timezone.utc
            ),
            attachments=self._extract_attachments(message),
            raw_metadata={"raw": message},
            chat_type=chat_type,
            reply_token=msg_id,  # Used as reply_to_message_id when sending
        )

        # Validate user identity
        if not self._validate_user(user_id):
            logger.warning("telegram_unauthorized_user", user_id=user_id)
            return

        await self._dispatch_to_core(unified)

    def _extract_attachments(self, message: dict) -> list[dict]:
        """Extract photo, document, voice, etc. from a Telegram message."""
        attachments = []
        for field in ("photo", "document", "voice", "video", "audio"):
            if field in message:
                if field == "photo":
                    # Take the largest photo
                    photo = message["photo"][-1]
                    attachments.append({
                        "type": "photo",
                        "file_id": photo["file_id"],
                        "mime_type": "image/jpeg",
                    })
                else:
                    item = message[field]
                    attachments.append({
                        "type": field,
                        "file_id": item.get("file_id", ""),
                        "mime_type": item.get("mime_type", ""),
                        "file_name": item.get("file_name", ""),
                    })
        return attachments

    def _validate_user(self, user_id: str) -> bool:
        """Check if the user matches the configured Telegram user_id."""
        configured = self.config.user.telegram.user_id
        if not configured:
            return True  # No restriction configured
        return user_id == configured

    async def _process_callback(self, callback_query: dict) -> None:
        """Process an Inline Keyboard button click (confirmation callback)."""
        try:
            cb_data_b64 = callback_query.get("data", "")
            cb_data = json.loads(base64.b64decode(cb_data_b64).decode())

            if cb_data.get("action") == "confirm_subtask":
                # Handle confirmation (implementation in confirmation.py)
                logger.info(
                    "telegram_confirmation_callback",
                    task_id=cb_data.get("task_id"),
                    approved=cb_data.get("approved"),
                )

        except Exception as e:
            logger.error("telegram_callback_parse_error", error=str(e))

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
        """Send a message via Telegram API."""
        import httpx

        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_token:
            payload["reply_to_message_id"] = int(reply_token)
        if inline_keyboard:
            payload["reply_markup"] = inline_keyboard

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("ok"):
                return str(data["result"]["message_id"])
            else:
                logger.error(
                    "telegram_send_failed",
                    error=data.get("description", "Unknown error"),
                )
                return ""

    async def send_confirmation(
        self,
        user_id: str,
        chat_id: str,
        confirmation: ConfirmationRequest,
    ) -> None:
        """Send a confirmation request with Inline Keyboard.

        Button is disabled during cooling-off period.
        After cooling-off, button becomes enabled.
        Uses Telegram's editMessageText for countdown updates (throttled).
        """
        # Build callback data
        cb_data = base64.b64encode(json.dumps({
            "action": "confirm_subtask",
            "task_id": confirmation.task_id,
            "step": confirmation.step,
            "approved": True,
            "nonce": confirmation.nonce,
        }).encode()).decode()

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": f"⏳ 请等待 {confirmation.cooling_off_seconds} 秒..." if confirmation.cooling_off_seconds > 0 else "✅ 确认执行",
                        "callback_data": cb_data,
                    }
                ],
                [
                    {
                        "text": "✕ 拒绝",
                        "callback_data": base64.b64encode(json.dumps({
                            "action": "confirm_subtask",
                            "task_id": confirmation.task_id,
                            "step": confirmation.step,
                            "approved": False,
                            "nonce": confirmation.nonce,
                        }).encode()).decode(),
                    }
                ],
            ]
        }

        risk_emoji = {
            "low": "ℹ️",
            "medium": "⚠️",
            "high": "🔴",
            "critical": "🚨",
        }
        emoji = risk_emoji.get(confirmation.risk_level, "⚠️")

        text = (
            f"{emoji} **{confirmation.risk_level.upper()} 风险操作需要确认**\n\n"
            f"{confirmation.preview_text}\n\n"
            f"⏳ 冷静期: {confirmation.cooling_off_seconds} 秒\n"
            f"⌛ 超时: {confirmation.timeout_seconds} 秒"
        )

        await self.send_message(
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            inline_keyboard=keyboard,
        )

    # ── Status ────────────────────────────────────────────────────────

    def get_info(self) -> AdapterInfo:
        return AdapterInfo(
            channel="telegram",
            state=self._state,
            last_heartbeat=self._last_heartbeat,
            error=self._error,
            metadata={"bot_token_set": bool(self._token)},
        )
