"""Device WebSocket endpoint for Host Agent connections.

Endpoint: ws://host:8000/api/v1/ws/device/{device_id}
Auth: PSK (Pre-Shared Key) via DEVICE_PSK env var
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from athena.api.deps import get_config_dep
from athena.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["device"])


@router.websocket("/ws/device/{device_id}")
async def device_websocket(
    websocket: WebSocket,
    device_id: str,
) -> None:
    """WebSocket endpoint for Device Agent connections.

    Authentication: PSK challenge-response on connect.
    Messages are JSON-RPC style: {"id": "...", "method": "...", "params": {...}}
    """
    config = get_config_dep()
    psk = config.device_psk

    await websocket.accept()
    logger.info("device_ws_connected", device_id=device_id)

    if not psk:
        logger.warning("device_psk_not_configured")
        await websocket.send_json({
            "id": None,
            "error": {"code": -1, "message": "PSK not configured on server"},
        })
        await websocket.close()
        return

    try:
        # PSK challenge-response authentication
        import secrets
        challenge = secrets.token_hex(32)

        await websocket.send_json({
            "id": "auth",
            "method": "challenge",
            "params": {"challenge": challenge},
        })

        response = await websocket.receive_json()
        expected = hmac.new(
            psk.encode(), challenge.encode(), hashlib.sha256
        ).hexdigest()

        if response.get("params", {}).get("response") != expected:
            logger.warning("device_auth_failed", device_id=device_id)
            await websocket.send_json({
                "id": "auth",
                "error": {"code": -2, "message": "Authentication failed"},
            })
            await websocket.close()
            return

        logger.info("device_authenticated", device_id=device_id)

        # Main message loop
        while True:
            data = await websocket.receive_json()
            msg_id = data.get("id", "")
            method = data.get("method", "")

            logger.debug(
                "device_command",
                device_id=device_id,
                method=method,
                msg_id=msg_id,
            )

            # Handle common commands
            if method == "ping":
                await websocket.send_json({
                    "id": msg_id,
                    "result": {"pong": True},
                })
            elif method == "heartbeat":
                # Update device last_heartbeat
                await websocket.send_json({
                    "id": msg_id,
                    "result": {"ack": True},
                })
            else:
                # Forward to handler (run_script, screenshot, etc.)
                await websocket.send_json({
                    "id": msg_id,
                    "result": {"status": "received", "method": method},
                })

    except WebSocketDisconnect:
        logger.info("device_ws_disconnected", device_id=device_id)
    except Exception as e:
        logger.error("device_ws_error", device_id=device_id, error=str(e))
