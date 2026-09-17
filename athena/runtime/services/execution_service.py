"""Harness 执行与后处理服务。

从 ``LangGraphRuntime`` 提取出的 Harness 执行、结果序列化和后处理逻辑。
"""

from __future__ import annotations

from typing import Any

from athena.config.settings import Settings
from athena.core.compression.compressor import ContextCompressor
from athena.core.harness.harness import Harness, HarnessRunResult, HarnessSettings
from athena.core.llm.provider import LLMProvider
from athena.core.memory.long_term_memory import LongTermMemoryService
from athena.core.memory.distillation import ConversationSummarizer
from athena.core.memory.contracts import CompletedTurn
from athena.infrastructure.sqlite.memory_job_repository import MemoryJobRepository
from athena.core.tools.manager import UnifiedToolManager
from athena.infrastructure.sqlite.database import Database
from athena.contracts.ports import EventPublisherPort
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
        memory_manager: LongTermMemoryService,
        conversation_summarizer: ConversationSummarizer,
        memory_job_repository: MemoryJobRepository,
        settings: Settings,
        event_publisher: EventPublisherPort,
    ) -> None:
        self._llm = llm
        self._tool_manager = tool_manager
        self._db = db
        self._compressor = compressor
        self._memory_manager = memory_manager
        self._conversation_summarizer = conversation_summarizer
        self._memory_job_repository = memory_job_repository
        self._settings = settings
        self._events = event_publisher

    async def run_harness(
        self,
        messages: list[Message],
        session_id: str,
        task_spec: dict[str, Any] | None = None,
        context_bundle: dict[str, Any] | None = None,
        run_id: str = "",
        stop_signal: Any = None,
    ) -> HarnessRunResult:
        """创建 Harness 实例并执行完整的 LLM/工具循环。

        参数：
            messages: 已构建的会话消息列表（包含历史、当前用户消息和文件引用）。
            session_id: 会话唯一标识。
            task_spec: 当前任务理解结果，注入系统提示。
            context_bundle: 检索到的上下文包，注入系统提示。
            run_id: 运行唯一标识。
            stop_signal: 停止事件，置位时终止执行。

        返回值：
            ``HarnessRunResult``：包含 LLM 输出内容、轮次数、工具调用结果等信息。
        """
        harness_settings = HarnessSettings(
            max_turns_per_run=self._settings.max_turns_per_run,
            retry_budget=self._settings.retry_budget,
            tool_timeout=self._settings.tool_timeout,
            approval_timeout=self._settings.approval_timeout,
        )

        system_prompt = self._build_system_prompt(task_spec, context_bundle)

        harness = Harness(
            llm=self._llm,
            tool_manager=self._tool_manager,
            settings=self._settings,
            db=self._db,
            event_publisher=self._events,
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
    def _build_system_prompt(
        task_spec: dict[str, Any] | None = None,
        context_bundle: dict[str, Any] | None = None,
    ) -> str:
        """使用内置系统提示词，并注入任务理解和检索上下文。"""
        system = get_prompt("system")
        if task_spec:
            goal = task_spec.get("goal", "")
            task_type = task_spec.get("task_type", "")
            requirements = ", ".join(task_spec.get("context_requirements", []))
            system += (
                "\n\n[Task Understanding]\n"
                f"goal: {goal}\n"
                f"task_type: {task_type}\n"
                f"context_requirements: {requirements}"
            )
        if context_bundle:
            sections = {
                "memory": [],
                "knowledge": [],
                "file": [],
                "conversation": [],
                "external": [],
            }
            for item in context_bundle.get("items", []):
                provider = item.get("provider", "")
                content = item.get("content", "")
                source = item.get("title") or item.get("source_id") or ""
                locator = item.get("locator", {})
                locator_text = (
                    f" locator={locator}" if locator else ""
                )
                sections.setdefault(provider, []).append(
                    f"- {f'source: {source}{locator_text}\\n  ' if source else ''}{content}"
                )
            blocks = []
            for provider, lines in sections.items():
                if lines:
                    blocks.append(f"[{provider.capitalize()}]\n" + "\n".join(lines))
            if blocks:
                system += "\n\n[Retrieved Context]\n" + "\n\n".join(blocks)
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
        try:
            await self._memory_job_repository.enqueue(turn.model_dump(mode="json"))
        except Exception as exc:
            # 记忆属于回答后的增强流程，队列暂时不可用时不能回滚已经生成的回答。
            logger.warning("memory_job_enqueue_failed", turn_id=turn_id, error=str(exc))
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
            "error_detail": result.error_detail,
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
