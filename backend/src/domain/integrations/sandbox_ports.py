"""沙箱执行端口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class SandboxPort(Protocol):
    async def execute(self, request: Any) -> Any: ...
    def build_mcp_process(
        self,
        *,
        server_name: str,
        image: str,
        command: list[str],
        args: list[str],
        workspace: Path,
        env: dict[str, str] | None = None,
        network_policy: str = "none",
    ) -> Any: ...
    async def health(self) -> bool: ...
    async def close_all(self) -> None: ...


__all__ = ["SandboxPort"]
