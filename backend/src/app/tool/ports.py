"""Run 工具运行时的应用层端口。

该协议描述 Run 执行器依赖的工具授权、目录与单次调用能力；实现位于
应用层，并组合知识、记忆与 MCP 等用例，因此不放在领域层。
"""

from __future__ import annotations

from typing import Any, Protocol

from domain.tool import (
    AuthorizedRunContext,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolOutcome,
)


class RunToolRuntime(Protocol):
    """Application boundary for authorization, catalog, and single calls."""

    async def authorize(
        self,
        owner_id: str,
        chat_session_id: str,
        run_id: str,
        auth_session_id: str | None,
    ) -> AuthorizedRunContext | None:
        """Re-read authentication and visible chat state before tools or delivery."""

    async def build_snapshot(
        self,
        prompt_template_version: str,
        context: AuthorizedRunContext,
    ) -> RuntimeToolSnapshot:
        """Build a bounded catalog without relying on a default page size."""

    async def execute(
        self,
        definition: RuntimeToolDefinition | None,
        model_name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        *,
        deadline_seconds: float,
        run_remaining_seconds: float,
    ) -> ToolExecutionResult:
        """Validate and execute one call within distinct tool and Run deadlines."""

    def validate_call(
        self,
        definition: RuntimeToolDefinition | None,
        model_name: str,
        arguments: dict[str, Any],
    ) -> ToolOutcome | None:
        """Return a safe local error before approval is requested."""


__all__ = ["RunToolRuntime"]
