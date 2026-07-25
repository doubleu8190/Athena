"""WeChat iLink Bot adapter — Long Poll via ClawBot (openclaw-weixin).

⚠️ EXPERIMENTAL: Relies on third-party reverse-engineered iLink protocol.
See the technical design document for risks and alternatives.

Communication pattern:
- QR code authentication flow
- Token persistence to /data/wechat_credentials.json (0600)
- Long Poll via POST /ilink/bot/getupdates (40s timeout)
- Text-based confirmations (no button support)
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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


@dataclass
class WeChatCredentials:
    """Persisted WeChat iLink bot credentials."""
    bot_token: str = ""
    ilink_bot_id: str = ""
    obtained_at: str = ""
    expires_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> WeChatCredentials:
        return cls(
            bot_token=data.get("bot_token", ""),
            ilink_bot_id=data.get("ilink_bot_id", ""),
            obtained_at=data.get("obtained_at", ""),
            expires_at=data.get("expires_at", ""),
        )

    def to_dict(self) -> dict:
        return {
            "bot_token": self.bot_token,
            "ilink_bot_id": self.ilink_bot_id,
            "obtained_at": self.obtained_at,
            "expires_at": self.expires_at,
        }


# ── Constants ─────────────────────────────────────────────────────────
WECHAT_CREDENTIALS_PATH = Path("/data/wechat_credentials.json")
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"


class WeChatAdapter(BaseIMAdapter):
    """WeChat iLink Bot adapter.

    Handles QR code authentication, credential persistence,
    Long Poll message reception, and text-based confirmations.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._state = AdapterState.DISCONNECTED
        self._last_heartbeat: datetime | None = None
        self._error: str | None = None
        self._poll_task: asyncio.Task | None = None
        self._running = False
        self._bot_token: str = ""
        self._ilink_bot_id: str = ""
        self._get_updates_buf: str = ""

        # Confirmation tracking
        self._pending_confirmations: dict[str, ConfirmationRequest] = {}

    @property
    def channel(self) -> str:
        return "wechat"

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize the WeChat adapter.

        Tries to restore persisted credentials first.
        Falls back to QR code authentication if credentials are expired.
        """
        self._running = True

        # Try to load persisted credentials
        if await self._load_credentials():
            self._state = AdapterState.CONNECTED
            logger.info("wechat_credentials_restored")
        else:
            # Check for env-configured credentials
            env_token = get_secret("WECHAT_BOT_TOKEN", "")
            env_bot_id = get_secret("WECHAT_ILINK_BOT_ID", "")
            if env_token and env_bot_id:
                self._bot_token = env_token
                self._ilink_bot_id = env_bot_id
                self._state = AdapterState.CONNECTED
                logger.info("wechat_credentials_from_env")
            else:
                # Start QR auth flow
                self._state = AdapterState.AUTHENTICATING
                success = await self._qr_auth_flow()
                if not success:
                    self._state = AdapterState.FAILED
                    self._error = "QR authentication failed"
                    return
                self._state = AdapterState.CONNECTED

        # Start Long Poll
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("wechat_started")

    async def stop(self) -> None:
        """Gracefully stop the adapter."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._state = AdapterState.DISCONNECTED
        logger.info("wechat_stopped")

    # ── Credential management ─────────────────────────────────────────

    async def _load_credentials(self) -> bool:
        """Load persisted credentials from /data/wechat_credentials.json."""
        if not WECHAT_CREDENTIALS_PATH.exists():
            return False

        try:
            data = json.loads(WECHAT_CREDENTIALS_PATH.read_text())
            creds = WeChatCredentials.from_dict(data)
            expires_at = datetime.fromisoformat(creds.expires_at)

            if datetime.now(UTC) >= expires_at:
                logger.info("wechat_credentials_expired")
                return False

            self._bot_token = creds.bot_token
            self._ilink_bot_id = creds.ilink_bot_id
            return True
        except Exception as e:
            logger.warning("wechat_credentials_load_failed", error=str(e))
            return False

    async def _save_credentials(self) -> None:
        """Persist credentials to /data/wechat_credentials.json (0600)."""
        WECHAT_CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        creds = WeChatCredentials(
            bot_token=self._bot_token,
            ilink_bot_id=self._ilink_bot_id,
            obtained_at=datetime.now(UTC).isoformat(),
            expires_at=datetime.now(UTC).isoformat(),
        )
        WECHAT_CREDENTIALS_PATH.write_text(json.dumps(creds.to_dict(), indent=2))
        WECHAT_CREDENTIALS_PATH.chmod(0o600)
        logger.info("wechat_credentials_saved")

    # ── QR Authentication ─────────────────────────────────────────────

    async def _qr_auth_flow(self) -> bool:
        """Complete the QR code authentication flow.

        Steps:
        1. GET /ilink/bot/get_bot_qrcode?bot_type=3
        2. Poll /ilink/bot/get_qrcode_status until confirmed or expired
        3. On confirmed: store bot_token and ilink_bot_id
        Max 3 retries on expiry.
        """
        import httpx

        for attempt in range(3):
            async with httpx.AsyncClient(base_url=ILINK_BASE_URL) as client:
                # Step 1: Get QR code
                resp = await client.get(
                    "/ilink/bot/get_bot_qrcode",
                    params={"bot_type": "3"},
                )
                resp.raise_for_status()
                qr_data = resp.json()
                qrcode_id = qr_data.get("qrcode")
                logger.info("wechat_qr_obtained", qrcode_id=qrcode_id)

                # Step 2: Poll for scan status
                status = await self._poll_qr_status(client, qrcode_id)
                if status == "confirmed":
                    return True
                elif status == "expired":
                    logger.info("wechat_qr_expired", attempt=attempt + 1)
                    continue
                else:
                    return False

        return False

    async def _poll_qr_status(self, client, qrcode_id: str) -> str:
        """Poll QR code scan status. Returns 'wait', 'scaned', 'confirmed', or 'expired'."""

        for _ in range(120):  # Max 2 minutes
            resp = await client.get(
                "/ilink/bot/get_qrcode_status",
                params={"qrcode": qrcode_id},
            )
            data = resp.json()
            status = data.get("status", "wait")

            if status == "confirmed":
                self._bot_token = data.get("bot_token", "")
                self._ilink_bot_id = data.get("ilink_bot_id", "")
                await self._save_credentials()
                logger.info("wechat_qr_confirmed")
                return "confirmed"
            elif status == "scaned":
                logger.info("wechat_qr_scaned_waiting_confirm")
            elif status == "expired":
                return "expired"

            await asyncio.sleep(1)

        return "expired"

    # ── Long Poll ────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main Long Poll loop via POST /ilink/bot/getupdates."""
        import httpx

        backoff = 5

        while self._running:
            try:
                headers = self._build_headers()
                payload = {"get_updates_buf": self._get_updates_buf}

                async with httpx.AsyncClient(
                    base_url=ILINK_BASE_URL,
                    timeout=httpx.Timeout(40),
                ) as client:
                    resp = await client.post(
                        "/ilink/bot/getupdates",
                        json=payload,
                        headers=headers,
                    )

                    if resp.status_code == 401:
                        # Token expired
                        logger.warning("wechat_token_expired")
                        self._state = AdapterState.DISCONNECTED
                        # Trigger re-auth
                        self._state = AdapterState.AUTHENTICATING
                        if not await self._qr_auth_flow():
                            self._state = AdapterState.FAILED
                            self._error = "Re-authentication failed"
                            return
                        self._state = AdapterState.CONNECTED
                        continue

                    resp.raise_for_status()
                    data = resp.json()

                    # Process messages
                    for msg in data.get("messages", []):
                        await self._process_message(msg)

                    # Update cursor
                    self._get_updates_buf = data.get("get_updates_buf", self._get_updates_buf)
                    self._last_heartbeat = datetime.now(UTC)
                    self._state = AdapterState.CONNECTED
                    self._error = None
                    backoff = 5

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("wechat_poll_error", error=str(e))
                self._state = AdapterState.RECONNECTING
                self._error = str(e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _process_message(self, raw: dict) -> None:
        """Convert a WeChat iLink message to UnifiedMessage and dispatch."""
        msg_id = raw.get("client_id", "")
        from_user_id = raw.get("from_user_id", "")
        context_token = raw.get("context_token", "")

        # Extract text content
        content = ""
        attachments = []
        for item in raw.get("item_list", []):
            if item.get("type") == "TEXT":
                content += item.get("text_item", {}).get("text", "")
            else:
                attachments.append(item)

        # Check if this is a confirmation reply
        # Use user_id to match pending confirmations
        if from_user_id in self._pending_confirmations:
            await self._handle_confirmation_reply(
                from_user_id, content
            )
            return

        unified = UnifiedMessage(
            message_id=msg_id,
            channel="wechat",
            user_id=from_user_id,
            content=content,
            timestamp=datetime.now(UTC),
            attachments=attachments,
            raw_metadata={
                "context_token": context_token,
                "raw": raw,
            },
            chat_type="private",
            reply_token=context_token,
        )

        # Validate user
        if not self._validate_user(from_user_id):
            logger.warning("wechat_unauthorized_user", user_id=from_user_id)
            return

        # Fire-and-forget: dispatch to core without blocking the poll loop
        asyncio.create_task(self._dispatch_to_core(unified))

    def _validate_user(self, user_id: str) -> bool:
        """Check if the user matches the configured WeChat user_id."""
        configured = self.config.user.wechat.user_id
        if not configured:
            return True
        return user_id == configured

    # ── Message sending ───────────────────────────────────────────────

    def _build_headers(self) -> dict:
        """Build common HTTP headers for iLink API requests."""
        random_uin = base64.b64encode(
            struct.pack("I", secrets.randbits(32))
        ).decode()
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self._bot_token}",
            "X-WECHAT-UIN": random_uin,
        }

    async def send_message(
        self,
        user_id: str,
        chat_id: str,
        text: str,
        reply_token: str | None = None,
        inline_keyboard: dict | None = None,
        attachments: list[dict] | None = None,
    ) -> str:
        """Send a text message via iLink API.

        Must include context_token (from reply_token) for conversation threading.
        """
        import httpx

        headers = self._build_headers()
        payload: dict = {
            "from_user_id": self._ilink_bot_id,
            "to_user_id": user_id,
            "item_list": [{"type": "TEXT", "text_item": {"text": text}}],
        }
        if reply_token:
            payload["context_token"] = reply_token

        async with httpx.AsyncClient(base_url=ILINK_BASE_URL) as client:
            resp = await client.post(
                "/ilink/bot/sendmessage",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("client_id", "")

    async def send_confirmation(
        self,
        user_id: str,
        chat_id: str,
        confirmation: ConfirmationRequest,
    ) -> None:
        """Send a text-based confirmation request.

        WeChat iLink does not support interactive buttons.
        User replies with "确认" / "取消" (or English equivalents).
        """
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
            f"⌛ 超时: {confirmation.timeout_seconds} 秒\n\n"
            f"回复 **确认** 执行此操作\n"
            f"回复 **取消** 放弃此操作\n\n"
            f"[操作编号: #task-{confirmation.task_id}]"
        )

        # Track for confirmation reply handling
        # Use user_id as key since WeChat users can only have one pending confirmation at a time
        self._pending_confirmations[user_id] = confirmation

        # Set timeout
        asyncio.create_task(self._confirmation_timeout(confirmation))

        await self.send_message(user_id, chat_id, text)

    async def _handle_confirmation_reply(
        self,
        user_id: str,
        content: str,
    ) -> None:
        """Handle a text reply to a confirmation request."""
        confirmation = self._pending_confirmations.pop(user_id, None)
        if not confirmation:
            return

        content_lower = content.strip().lower()
        approved = content_lower in ("确认", "confirm", "yes", "是")

        if approved:
            logger.info(
                "wechat_confirmation_approved",
                task_id=confirmation.task_id,
            )
        else:
            logger.info(
                "wechat_confirmation_rejected",
                task_id=confirmation.task_id,
            )

        # Call GatewayManager to resume graph execution
        try:
            from athena.gateway.manager import get_gateway_manager
            gateway_manager = get_gateway_manager()
            await gateway_manager.handle_confirmation_response(
                channel="wechat",
                user_id=user_id,
                chat_id=confirmation.chat_id,
                session_id=confirmation.task_id,
                approved=approved,
            )
        except Exception as e:
            logger.error(
                "wechat_confirmation_resume_error",
                task_id=confirmation.task_id,
                error=str(e),
            )

    async def _confirmation_timeout(self, confirmation: ConfirmationRequest) -> None:
        """Handle confirmation timeout."""
        await asyncio.sleep(confirmation.timeout_seconds)
        # Use user_id to remove timed-out confirmation
        self._pending_confirmations.pop(confirmation.user_id, None)

    # ── Status ────────────────────────────────────────────────────────

    def get_info(self) -> AdapterInfo:
        return AdapterInfo(
            channel="wechat",
            state=self._state,
            last_heartbeat=self._last_heartbeat,
            error=self._error,
            metadata={
                "bot_token_set": bool(self._bot_token),
                "ilink_bot_id": self._ilink_bot_id,
            },
        )
