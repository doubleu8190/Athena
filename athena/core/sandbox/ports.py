"""Sandbox runner port used by tools and MCP adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from athena.core.sandbox.models import (
    ExecutionRequest,
    ExecutionResult,
    MCPProcessSpec,
    SandboxRunHandle,
    SandboxRunRequest,
)


class SandboxRunner(Protocol):
    async def ensure_run(self, request: SandboxRunRequest) -> SandboxRunHandle:
        ...

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        ...

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
    ) -> MCPProcessSpec:
        ...

    def resolve_mcp_image(self, image_id: str) -> str:
        ...

    async def close_run(self, run_id: str) -> None:
        ...

    async def close_all(self) -> None:
        ...

    async def health(self) -> bool:
        ...
