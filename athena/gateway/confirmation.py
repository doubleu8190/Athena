"""Confirmation flow coordinator.

Handles the end-to-end confirmation lifecycle:
- Cooling-off timers per risk level
- Timeout handling
- Nonce-based replay protection
- Per-channel UI coordination via Gateway adapters
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass

from athena.gateway.base import ConfirmationRequest
from athena.logging_config import get_logger

logger = get_logger(__name__)


# ── Confirmation state tracking ───────────────────────────────────────

@dataclass
class ConfirmationState:
    """Tracks the state of an in-flight confirmation."""
    request: ConfirmationRequest
    sent_at: float
    cooling_off_until: float
    timeout_at: float
    status: str = "pending"  # 'pending', 'cooling_off', 'ready', 'confirmed', 'rejected', 'timeout'


class ConfirmationManager:
    """Manages the confirmation lifecycle across all channels.

    Coordinates:
    - Cooling-off timers (enforced server-side as well as client-side)
    - Timeout detection and automatic cancellation
    - Nonce validation for replay protection
    """

    def __init__(self) -> None:
        self._confirmations: dict[str, ConfirmationState] = {}
        self._nonces_seen: set[str] = set()

    # ── Confirmation lifecycle ────────────────────────────────────────

    async def create_confirmation(
        self,
        task_id: str,
        step: int,
        risk_level: str,
        preview_text: str,
        cooling_off_seconds: int,
        timeout_seconds: int,
        channel: str,
        user_id: str,
        chat_id: str,
    ) -> ConfirmationRequest:
        """Create a new confirmation request.

        Args:
            task_id: Parent task ID.
            step: Subtask step number.
            risk_level: 'low', 'medium', 'high', or 'critical'.
            preview_text: Human-readable operation summary.
            cooling_off_seconds: Mandatory wait before confirmation.
            timeout_seconds: Auto-cancel after this duration.
            channel: IM channel for delivery.
            user_id: Target user.
            chat_id: Target chat.

        Returns:
            A ConfirmationRequest ready for Gateway delivery.
        """
        nonce = secrets.token_hex(32)  # 64 hex chars

        confirmation = ConfirmationRequest(
            task_id=task_id,
            step=step,
            risk_level=risk_level,
            preview_text=preview_text,
            cooling_off_seconds=cooling_off_seconds,
            timeout_seconds=timeout_seconds,
            nonce=nonce,
            channel=channel,
            user_id=user_id,
            chat_id=chat_id,
        )

        now = time.time()
        state = ConfirmationState(
            request=confirmation,
            sent_at=now,
            cooling_off_until=now + cooling_off_seconds,
            timeout_at=now + timeout_seconds,
            status="cooling_off" if cooling_off_seconds > 0 else "ready",
        )

        self._confirmations[nonce] = state

        logger.info(
            "confirmation_created",
            task_id=task_id,
            step=step,
            risk_level=risk_level,
            cooling_off=cooling_off_seconds,
            timeout=timeout_seconds,
            nonce=nonce,
        )

        # Schedule timeout
        asyncio.create_task(self._schedule_timeout(nonce, timeout_seconds))

        return confirmation

    # ── Response handling ─────────────────────────────────────────────

    async def handle_response(
        self,
        nonce: str,
        approved: bool,
    ) -> str:
        """Handle a user's response to a confirmation request.

        Returns: 'confirmed', 'rejected', 'timeout', 'invalid_nonce', 'cooling_off_not_elapsed'
        """
        # Validate nonce (replay protection)
        if nonce in self._nonces_seen:
            logger.warning("confirmation_nonce_replay", nonce=nonce)
            return "invalid_nonce"

        state = self._confirmations.get(nonce)
        if not state:
            return "invalid_nonce"

        # Record nonce usage
        self._nonces_seen.add(nonce)

        # Check timeout
        if time.time() > state.timeout_at:
            state.status = "timeout"
            logger.info("confirmation_timeout_on_response", nonce=nonce)
            return "timeout"

        # Handle rejection (allowed regardless of cooling-off)
        if not approved:
            state.status = "rejected"
            logger.info("confirmation_rejected", nonce=nonce)
            return "rejected"

        # Handle approval — check cooling-off
        remaining = state.cooling_off_until - time.time()
        if remaining > 0:
            logger.info(
                "confirmation_cooling_off_not_elapsed",
                nonce=nonce,
                remaining=remaining,
            )
            return "cooling_off_not_elapsed"

        state.status = "confirmed"
        logger.info("confirmation_confirmed", nonce=nonce)
        return "confirmed"

    async def _schedule_timeout(self, nonce: str, timeout_seconds: int) -> None:
        """Background task: auto-cancel on timeout."""
        await asyncio.sleep(timeout_seconds)
        state = self._confirmations.get(nonce)
        if state and state.status in ("pending", "cooling_off", "ready"):
            state.status = "timeout"
            logger.info(
                "confirmation_timed_out",
                nonce=nonce,
                task_id=state.request.task_id,
            )

    # ── Queries ───────────────────────────────────────────────────────

    def get_state(self, nonce: str) -> ConfirmationState | None:
        """Get the current state of a confirmation."""
        return self._confirmations.get(nonce)

    def cleanup(self, nonce: str) -> None:
        """Remove a confirmation from tracking (after it's resolved)."""
        self._confirmations.pop(nonce, None)
