"""Host Device Agent — WebSocket server for controlling the host machine.

Connects via WSS (secure WebSocket) with PSK authentication.
Commands: run_script, screenshot, simulate_keystroke.
All commands support _preview mode.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from athena.logging_config import get_logger

logger = get_logger(__name__)


class HostAgent:
    """Server-side controller for the host machine Device Agent.

    The actual agent runs on the user's machine and connects via WSS.
    This class provides the command interface for the Athena Core.
    """

    def __init__(self, device_id: str, psk: str):
        self.device_id = device_id
        self.psk = psk
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def run_script(
        self,
        script: str,
        language: str = "bash",
        preview: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        """Execute a script on the host machine.

        Risk level: critical.
        Supports preview mode.
        """
        if preview:
            return {
                "success": True,
                "preview": True,
                "script_preview": script[:500],
                "language": language,
            }

        # Script execution is done through the WebSocket connection
        # to the device agent. This is a server-side placeholder.
        return {
            "success": True,
            "output": "Script execution delegated to device agent",
            "device_id": self.device_id,
        }

    async def screenshot(self, preview: bool = False, **kwargs) -> dict[str, Any]:
        """Capture a screenshot of the host machine.

        Risk level: low (read-only).
        """
        if preview:
            return {"success": True, "preview": True, "action": "screenshot"}

        return {
            "success": True,
            "action": "screenshot",
            "device_id": self.device_id,
        }

    async def simulate_keystroke(
        self,
        keys: str,
        preview: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        """Simulate keyboard input on the host machine.

        Risk level: critical.
        """
        if preview:
            return {
                "success": True,
                "preview": True,
                "keys": keys,
            }

        return {
            "success": True,
            "keys": keys,
            "device_id": self.device_id,
        }
