"""ADB Manager — Android device control via ADB.

Manages device registry and wraps ADB commands as MCP tools.
Devices must be on the same LAN and ADB-connectable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from athena.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ADBDevice:
    """An ADB-connected Android device."""
    serial: str
    model: str = ""
    status: str = "offline"  # 'online', 'offline', 'unauthorized'
    android_version: str = ""
    last_seen: str = ""


class ADBManager:
    """Manages Android devices and provides ADB command wrappers.

    Commands are exposed as MCP tools: adb_shell, adb_screenshot,
    adb_install, adb_tap.
    """

    def __init__(self):
        self._devices: dict[str, ADBDevice] = {}

    # ── Device registry ───────────────────────────────────────────────

    async def scan_devices(self) -> list[ADBDevice]:
        """Scan for connected ADB devices."""
        import asyncio
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "devices", "-l",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            lines = stdout.decode().strip().split("\n")[1:]  # Skip header

            devices = []
            for line in lines:
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    serial = parts[0]
                    status = parts[1]
                    device = ADBDevice(serial=serial, status=status)

                    # Parse device info
                    for part in parts[2:]:
                        if part.startswith("model:"):
                            device.model = part.split(":", 1)[1]
                        elif part.startswith("device:"):
                            device.model = part.split(":", 1)[1]

                    self._devices[serial] = device
                    devices.append(device)

            return devices
        except FileNotFoundError:
            logger.warning("adb_not_found")
            return []
        except Exception as e:
            logger.error("adb_scan_failed", error=str(e))
            return []

    def get_device(self, serial: str) -> ADBDevice | None:
        return self._devices.get(serial)

    # ── ADB Commands (MCP tools) ─────────────────────────────────────

    async def adb_shell(
        self,
        serial: str,
        command: str,
        **kwargs,
    ) -> dict[str, Any]:
        """Execute a shell command on an Android device.

        Risk level: high.
        """
        import asyncio
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "-s", serial, "shell", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30.0
            )
            return {
                "success": proc.returncode == 0,
                "output": stdout.decode(errors="replace"),
                "error": stderr.decode(errors="replace") if stderr else None,
                "exit_code": proc.returncode,
            }
        except asyncio.TimeoutError:
            return {"success": False, "error": "Command timed out (30s)"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def adb_screenshot(
        self,
        serial: str,
        **kwargs,
    ) -> dict[str, Any]:
        """Capture a screenshot from an Android device.

        Risk level: low (read-only).
        """
        import asyncio
        import base64
        import tempfile
        from pathlib import Path

        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)

            proc = await asyncio.create_subprocess_exec(
                "adb", "-s", serial, "exec-out", "screencap", "-p",
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)

            if stdout:
                data = base64.b64encode(stdout).decode()
                return {
                    "success": True,
                    "screenshot_base64": data,
                    "format": "png",
                }
            return {"success": False, "error": "No screenshot data received"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def adb_install(
        self,
        serial: str,
        apk_path: str,
        **kwargs,
    ) -> dict[str, Any]:
        """Install an APK on an Android device.

        Risk level: critical.
        """
        import asyncio
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "-s", serial, "install", "-r", apk_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120.0
            )
            output = stdout.decode(errors="replace")
            return {
                "success": "Success" in output,
                "output": output,
                "error": stderr.decode(errors="replace") if stderr else None,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def adb_tap(
        self,
        serial: str,
        x: int,
        y: int,
        **kwargs,
    ) -> dict[str, Any]:
        """Simulate a tap on an Android device screen.

        Risk level: high.
        """
        return await self.adb_shell(
            serial,
            f"input tap {x} {y}",
        )
