"""Run 工具运行时的应用层端口。

该协议描述 Run 执行器依赖的工具授权、目录与单次调用能力；实现位于
应用层，并组合知识、记忆与 MCP 等用例，因此不放在领域层。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from domain.file import PendingSessionFile
from domain.run import Run, RunInputFileContext, RunInputImageAttachment
from domain.skill import RunSkillPromptContext
from domain.tool import (
    AuthorizedRunContext,
    RuntimeToolDefinition,
    RuntimeToolSnapshot,
    ToolCallPreparation,
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
        *,
        depth: int = 0,
        allowed_tool_ids: Sequence[str] | None = None,
        knowledge_base_ids: tuple[str, ...] | None = None,
    ) -> RuntimeToolSnapshot:
        """Build a bounded catalog without relying on a default page size."""

    async def prepare_call(
        self,
        definition: RuntimeToolDefinition | None,
        model_name: str,
        arguments: dict[str, Any],
        run: Run,
        context: AuthorizedRunContext | None,
        *,
        call_id: str,
    ) -> ToolCallPreparation:
        """校验一次调用并解析 Run 固定 Skill 审批/安全投影。"""

    async def build_skill_context(
        self,
        run: Run,
        loaded_skill_ids: tuple[str, ...],
    ) -> RunSkillPromptContext:
        """从 Run 快照和持久化加载证据重建不可压缩技能上下文。"""

    async def build_run_input_file_context(
        self,
        run: Run,
    ) -> tuple[RunInputFileContext, ...]:
        """按当前会话权限解析 Run 固定文件的安全引用元数据。"""

    async def build_run_input_vision_context(
        self,
        run: Run,
        input_files: Sequence[RunInputFileContext],
        *,
        supports_vision: bool,
    ) -> tuple[tuple[RunInputFileContext, ...], tuple[RunInputImageAttachment, ...]]:
        """按模型识图能力构建内存态视觉图片附件，并回填每张图片的安全 vision_status。"""

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

    async def abandon_artifacts(
        self,
        artifacts: tuple[PendingSessionFile, ...],
    ) -> None:
        """在工具结果落库失败时主动清理已上传但未发布的临时产物对象。"""

    async def can_read_subagent(self, user_id: str, subagent_id: str) -> bool:
        """调度子智能体前复核当前用户对目标 Subagent 的实时读权限。"""


__all__ = ["RunToolRuntime"]
