"""Harness 执行与后处理服务。

从 ``LangGraphRuntime`` 提取出的 Harness 执行、结果序列化和后处理逻辑。
"""

from __future__ import annotations

from typing import Any

from athena.config.settings import Settings
from athena.core.compression.compressor import ContextCompressor
from athena.core.harness.harness import Harness, HarnessRunResult, HarnessSettings
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
from athena.core.memory.summarizer import ConversationSummarizer
from athena.core.memory.contracts import CompletedTurn
from athena.infrastructure.sqlite.memory_job_repository import MemoryJobRepository
from athena.core.tools.manager import UnifiedToolManager
from athena.infrastructure.sqlite.database import Database
from athena.models import Message
from athena.models.file import AttachmentRef
from athena.utils.logging import get_logger
from athena.utils.prompts import get_prompt

logger = get_logger(__name__)


class ExecutionService:
    """Harness 执行编排与后处理服务。

    负责创建 Harness 实例、执行 LLM/工具循环、序列化结果为 JSON 安全格式，
    以及在执行完成后触发记忆写入和摘要生成。
    """

    def __init__(
        self,
        llm: LLMProvider,
        tool_manager: UnifiedToolManager,
        db: Database,
        compressor: ContextCompressor,
        memory_manager: MemoryManager,
        conversation_summarizer: ConversationSummarizer,
        memory_job_repository: MemoryJobRepository,
        settings: Settings,
    ) -> None:
        self._llm = llm
        self._tool_manager = tool_manager
        self._db = db
        self._compressor = compressor
        self._memory_manager = memory_manager
        self._conversation_summarizer = conversation_summarizer
        self._memory_job_repository = memory_job_repository
        self._settings = settings

    async def run_harness(
        self,
        messages: list[Message],
        session_id: str,
        memory_context: str = "",
        run_id: str = "",
        stop_signal: Any = None,
    ) -> HarnessRunResult:
        """创建 Harness 实例并执行完整的 LLM/工具循环。

        参数：
            messages: 已构建的会话消息列表（包含历史、当前用户消息和文件引用）。
            session_id: 会话唯一标识。
            memory_context: 从长期记忆中检索到的上下文，注入系统提示。
            run_id: 运行唯一标识。
            stop_signal: 停止事件，置位时终止执行。

        返回值：
            ``HarnessRunResult``：包含 LLM 输出内容、轮次数、工具调用结果等信息。
        """
        harness_settings = HarnessSettings(
            max_turns_per_run=self._settings.max_turns_per_run,
            retry_budget=self._settings.retry_budget,
            tool_timeout=self._settings.tool_timeout,
        )

        system_prompt = self._build_system_prompt(memory_context)

        harness = Harness(
            llm=self._llm,
            tool_manager=self._tool_manager,
            settings=self._settings,
            db=self._db,
            event_publisher=None,
            compressor=self._compressor,
            harness_settings=harness_settings,
        )

        return await harness.run(
            messages=[{"role": m.role.value, "content": m.content} for m in messages],
            session_id=session_id,
            system_prompt=system_prompt,
            run_id=run_id,
            stop_signal=stop_signal,
        )

    @staticmethod
    def _build_system_prompt(memory_context: str) -> str:
        """使用内置系统提示词，并在存在记忆上下文时追加记忆注入块。"""
        system = get_prompt("system")
        if memory_context:
            system += f"\n\n[Memory Context]\n{memory_context}"
        return system

    async def post_process(
        self,
        session_id: str,
        user_message: str,
        result: HarnessRunResult,
        *,
        turn_id: str,
    ) -> None:
        """在 Harness 执行完成后触发记忆写入和摘要检查。

        参数：
            session_id: 会话唯一标识。
            user_message: 本轮用户消息文本。
            result: Harness 执行返回的领域结果。
            turn_id: 本轮运行 ID。
        """
        turn = CompletedTurn(
            turn_id=turn_id,
            session_id=session_id,
            user_text=user_message,
            assistant_text=result.content,
        )
        await self._memory_job_repository.enqueue(turn.model_dump(mode="json"))
        try:
            await self._conversation_summarizer.summarize_if_needed(
                session_id=session_id, db=self._db
            )
        except Exception as e:
            logger.warning("summary_trigger_failed", error=str(e))

    @classmethod
    def result_payload(
        cls, result: HarnessRunResult, attachment_refs: list[AttachmentRef]
    ) -> dict[str, Any]:
        """将 Harness 运行结果序列化为 JSON 安全的字典负载。

        参数：
            result: Harness 执行返回的领域结果。
            attachment_refs: 本轮关联的附件引用列表。

        返回值：
            包含 content/run_id/turn_count/tool_results/error/interrupted/attachments 的字典。
        """
        return {
            "content": result.content,
            "run_id": result.run_id,
            "turn_count": result.turn_count,
            "tool_results": result.tool_results,
            "error": result.error,
            "interrupted": result.interrupted,
            "attachments": cls._serialize_attachments(attachment_refs),
        }

    @staticmethod
    def deserialize_attachment_refs(
        payload: list[dict[str, Any]],
    ) -> list[AttachmentRef]:
        """从 LangGraph 状态恢复附件轻量引用。"""
        return [AttachmentRef.model_validate(item) for item in payload]

    @staticmethod
    def deserialize_messages(payload: list[dict[str, Any]]) -> list[Message]:
        """从 LangGraph 状态恢复 Harness 消息。"""
        return [Message.model_validate(item) for item in payload]

    @staticmethod
    def _serialize_attachments(refs: list[AttachmentRef]) -> list[dict[str, Any]]:
        """将附件引用列表序列化为 JSON 安全字典列表。"""
        return [item.model_dump(mode="json") for item in refs]
