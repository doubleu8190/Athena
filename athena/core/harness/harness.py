"""Harness 执行引擎 — Agent 运行的核心编排器.

核心职责:
- 执行 LLM 调用（流式输出，支持超时兜底）
- 编排工具调用（asyncio.gather 并行执行无依赖工具）
- 预算控制（max_turns + retry_budget 双重限制）
- 发布应用事件到 Gateway
- 执行过程日志通过 Application Event 发布，并持久化消息与工具账本

执行流程:
    用户消息 → LLM 流式调用 → 工具调用（并行） → LLM 再调用 → ... → 终止

"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langchain_core.messages import (
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)

from athena.config.settings import Settings
from athena.core.compression.compressor import ContextCompressor
from athena.core.harness.budget import Budget, BudgetExceeded
from athena.core.harness.error_handler import ToolErrorHandler
from athena.core.llm.provider import LLMProvider
from athena.core.tools.manager import UnifiedToolManager
from athena.infrastructure.postgre.database import Database
from athena.models import Message, MessageRole, ToolCallRecord
from athena.models.tool import ToolCallStatus
from athena.contracts.events import EventType
from athena.contracts.errors import ExecutionError
from athena.contracts.events import ApplicationEvent, EventDurability
from athena.contracts.ports import EventPublisherPort
from athena.utils.id_generation import generate_time_id
from athena.utils.llm_response import extract_message_text
from athena.utils.logging import get_logger
from athena.utils.message_conversion import dict_to_message, normalize_tool_calls
from athena.runtime.stream_coalescer import StreamCoalescer

logger = get_logger(__name__)


@dataclass
class HarnessSettings:
    """Harness 运行时配置.

    属性：
        max_turns_per_run: 单次 run 的最大 LLM 调用轮次。
        retry_budget: 工具调用失败后的最大重试次数（工具成功后重置）。
        tool_timeout: 单个工具调用的超时时间（秒）。
        llm_stream_timeout: LLM 流式调用的整体超时时间（秒），
            防止流挂死导致 run 无法终止。
    """

    max_turns_per_run: int = 20
    retry_budget: int = 3
    tool_timeout: int = 60
    llm_stream_timeout: int = 120


@dataclass
class HarnessRunResult:
    """单次 Harness 运行结果.

    属性：
        content: 最后一轮 LLM 返回的文本内容。
        run_id: 本次运行的唯一标识。
        turn_count: 实际 LLM 调用轮次。
        tool_results: 所有工具调用的结果列表，每项包含
            tool_name / arguments / output / status / duration_ms / error。
        error: 错误信息（成功时为 None）。
        interrupted: 是否因用户停止信号而中断。
    """

    content: str
    run_id: str
    turn_count: int
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    error_detail: dict[str, Any] | None = None
    interrupted: bool = False


class Harness:
    """Agent 执行引擎 — 编排 LLM 调用与工具执行的核心循环.

    典型用法::

        harness = Harness(
            llm=provider,
            tool_manager=manager,
            settings=settings,
            db=db,
            event_publisher=publisher,
            compressor=compressor,
        )
        result = await harness.run(messages=msgs, session_id="s1")
    """

    def __init__(
        self,
        llm: LLMProvider,
        tool_manager: UnifiedToolManager,
        settings: Settings,
        db: Database,
        compressor: ContextCompressor,
        event_publisher: EventPublisherPort,
        error_handler: ToolErrorHandler | None = None,
        harness_settings: HarnessSettings | None = None,
    ) -> None:
        """初始化 Harness 实例.

        参数：
            llm: LLM Provider，负责模型调用（流式/非流式）。
            tool_manager: 统一工具管理器，负责工具注册与调用。
            settings: 全局配置。
            db: 数据库实例，用于持久化消息与工具调用账本。
            event_publisher: Runtime event publisher; no transport details leak into the harness.
            compressor: 上下文压缩器，在每轮 LLM 调用前压缩消息列表。
            error_handler: 工具错误自愈路由器（含熔断器）。未提供时使用默认实例。
            harness_settings: Harness 运行时配置。未提供时从全局 Settings 派生。
        """
        self._llm = llm
        self._tool_manager = tool_manager
        self._db = db
        self._event_publisher = event_publisher
        self._compressor = compressor
        self._error_handler = error_handler or ToolErrorHandler()
        self._harness_settings = harness_settings or HarnessSettings(
            max_turns_per_run=settings.max_turns_per_run,
            retry_budget=settings.retry_budget,
            tool_timeout=settings.tool_timeout,
            llm_stream_timeout=settings.llm_stream_timeout,
        )
        self._stop_signal: asyncio.Event | None = None
        self._allowed_tool_names: set[str] | None = None
        self._parent_run_id: str | None = None
        self._message_id: str | None = None
        self._answer_stream_id: str | None = None
        self._answer_stream: StreamCoalescer | None = None
        self._stable_tool_ids = False
        self._agent_role = "root"
        self._plan_id: str | None = None
        self._task_id: str | None = None
        self._depth = 0
        self._register_default_routes()

    def _register_default_routes(self) -> None:
        """注册默认 Fallback 路由.

        为 exec_shell 工具注册两条路由:
        - timeout: 自动重试 1 次，timeout 参数翻倍
        - permission_denied: 升级为用户可见错误（escalate）
        """
        self._error_handler.register_fallback(
            tool_name="exec_shell",
            on_errors=["timeout", "timed out"],
            action="retry",
            max_retries=1,
            param_transform={
                "timeout": lambda t: (t * 2) if isinstance(t, (int, float)) else t
            },
        )
        self._error_handler.register_fallback(
            tool_name="exec_shell",
            on_errors=["permission_denied"],
            action="escalate",
        )

    # ------------------------------------------------------------------
    # 主执行循环
    # ------------------------------------------------------------------

    async def run(
        self,
        messages: Sequence[Message | dict[str, Any]],
        session_id: str,
        system_prompt: str = "",
        run_id: str | None = None,
        parent_run_id: str | None = None,
        tool_names: list[str] | None = None,
        stop_signal: asyncio.Event | None = None,
        agent_role: str = "root",
        plan_id: str | None = None,
        task_id: str | None = None,
        depth: int = 0,
    ) -> HarnessRunResult:
        """执行单次 Agent 运行.

        主循环流程: LLM 流式调用 → 工具并行执行 → LLM 再调用 → ...
        终止条件: LLM 无工具调用 / 预算超限 / 停止信号 / 异常。

        参数：
            messages: 对话历史（含最新用户消息）。支持 Message 对象或 dict 格式。
            session_id: 会话 ID，用于关联消息与事件推送。
            system_prompt: 系统提示词，作为第一条 SystemMessage 注入。
            run_id: 可选 run_id（子 Agent 使用），未提供则自动生成。
            parent_run_id: 父 run_id（子 Agent 运行时指向其父 run；主 run 为 None），
                发布每个调用事件，使执行过程能显式追溯。
            tool_names: 可选工具白名单（子 Agent 使用），None 表示允许全部工具。
            stop_signal: 外部停止信号（会话级 stop 事件），由 gateway 层注入；
                置位后终止当前运行。

        返回值：
            HarnessRunResult，包含最终文本、run_id、轮次、工具结果、错误和中断状态。

        异常：
            BudgetExceeded: 预算超限且无剩余重试次数时抛出。
            异常: 未预期的执行异常（会被捕获并记录到 error 字段）。
        """
        rid = run_id or generate_time_id()
        self._parent_run_id = parent_run_id
        self._agent_role = agent_role
        self._plan_id = plan_id
        self._task_id = task_id
        self._depth = depth
        self._message_id = self._message_id_from_messages(messages)
        budget = Budget(
            max_turns=self._harness_settings.max_turns_per_run,
            retry_budget=self._harness_settings.retry_budget,
        )
        self._stop_signal = stop_signal

        # 更新会话状态为 running
        if parent_run_id is None:
            await self._db.sessions.update(session_id, status="running", run_id=rid)

        self._answer_stream_id = f"answer-{rid}"
        self._answer_stream = StreamCoalescer(
            session_id=session_id,
            run_id=rid,
            stream_id=self._answer_stream_id,
            stream_type="answer",
            message_id=self._message_id,
            publish_realtime=self._event_publisher.publish_realtime,
        )
        await self._emit(
            EventType.STREAM_START,
            {
                "run_id": rid,
                "stream_id": self._answer_stream_id,
                "stream_type": "answer",
            },
            session_id,
            rid,
        )
        await self._emit_thinking(
            EventType.THINKING_STARTED,
            "正在准备请求",
            session_id,
            rid,
        )

        # 压缩阶段使用领域消息；只有在调用 LLM 前才转换为 LangChain 消息。
        domain_messages: list[Message] = []
        for m in messages:
            if isinstance(m, Message):
                domain_messages.append(m)
                continue
            payload = dict(m)
            payload.setdefault("id", f"{rid}:message:{len(domain_messages)}")
            payload.setdefault("session_id", session_id)
            payload.setdefault("run_id", rid)
            payload.setdefault("timestamp", datetime.now())
            domain_messages.append(Message.model_validate(payload))

        # 绑定工具（子 Agent 按 tool_names 白名单过滤，None = 全部）
        self._allowed_tool_names = set(tool_names) if tool_names else None
        lc_tools = self._tool_manager.get_langchain_tools(names=tool_names)
        bound_llm = self._llm.bind_tools(lc_tools) if lc_tools else self._llm

        tool_results_all: list[dict[str, Any]] = []
        last_content = ""
        error_msg: str | None = None
        error_detail: ExecutionError | None = None
        interrupted = False

        try:
            while not self._should_stop():
                # 上下文压缩
                compressed_domain = await self._compressor.compress(
                    domain_messages,
                    session_id=session_id,
                )
                if len(compressed_domain) < len(domain_messages):
                    domain_messages = compressed_domain

                lc_messages: list[BaseMessage] = []
                if system_prompt:
                    lc_messages.append(SystemMessage(content=system_prompt))
                lc_messages.extend(
                    dict_to_message(message) for message in domain_messages
                )

                # LLM 调用事件使用稳定的调用 ID 关联生命周期。
                llm_call_id = generate_time_id()
                budget.increment_turn()

                await self._emit(
                    EventType.LLM_CALL_START,
                    {"call_id": llm_call_id, "turn": budget.turn_count},
                    session_id,
                    rid,
                )
                await self._emit_thinking(
                    EventType.THINKING_SUMMARY,
                    "正在生成回答",
                    session_id,
                    rid,
                )

                start_time = time.time()
                full_content = ""
                stream_chunks: list[AIMessageChunk] = []
                try:
                    # 流式调用整体包超时兜底：流挂死时无法依靠 stop_signal break
                    # 永远等不到 __anext__，只有超时能终态化当前 LLM 调用
                    async with asyncio.timeout(
                        self._harness_settings.llm_stream_timeout
                    ):
                        async for chunk in bound_llm.astream(lc_messages):
                            if self._should_stop():
                                break
                            if isinstance(chunk, AIMessageChunk):
                                stream_chunks.append(chunk)
                            chunk_content = extract_message_text(chunk)
                            if chunk_content:
                                full_content += chunk_content
                                if self._answer_stream is not None:
                                    await self._answer_stream.append(chunk_content)

                        # 合并流式 chunk → 完整响应（content + tool_calls）
                        merged_content, final_tc = self._assemble_response(
                            stream_chunks, full_content
                        )
                    if merged_content:
                        full_content = merged_content

                    # 停止信号在流式中途置位 → 结束本轮 run
                    # （不落库残缺 assistant 消息、不执行残缺 tool_calls；
                    #   必须终态化当前 LLM 调用并补发 LLM_CALL_END，
                    #   否则前端气泡无法结束）
                    if self._should_stop():
                        interrupted = True
                        error_detail = ExecutionError(
                            code="run_interrupted",
                            message="运行被用户中断",
                            error_type="CancelledError",
                            retryable=True,
                            phase="llm_call",
                        )
                        await self._emit_llm_call_end(
                            llm_call_id,
                            status="failed",
                            session_id=session_id,
                            run_id=rid,
                        )
                        break

                    # 空响应检测：无文本且无工具调用 → 不落库、有界重试
                    if not full_content.strip() and not final_tc:
                        error_msg = "LLM 返回空响应（无内容且无工具调用）"
                        error_detail = ExecutionError(
                            code="llm_empty_response",
                            message=error_msg,
                            error_type="LLMEmptyResponse",
                            retryable=budget.remaining_retries() > 0,
                            phase="llm_call",
                        )
                        logger.warning(
                            "llm_empty_response", run_id=rid, error=error_msg
                        )
                        await self._emit(
                            EventType.RUN_FAILED,
                            {
                                "call_id": llm_call_id,
                                "error": error_msg,
                                "phase": "llm_call",
                            },
                            session_id,
                            rid,
                        )
                        # 失败路径同样需结束 LLM 调用生命周期：前端据此移除
                        # 本次调用创建的流式气泡，避免重试残留空气泡
                        await self._emit_llm_call_end(
                            llm_call_id,
                            status="failed",
                            session_id=session_id,
                            run_id=rid,
                        )
                        if budget.remaining_retries() > 0:
                            budget.increment_retry()
                            continue
                        break

                    domain_messages.append(
                        Message(
                            id=generate_time_id(),
                            session_id=session_id,
                            role=MessageRole.ASSISTANT,
                            content=full_content,
                            tool_calls=final_tc,
                            run_id=rid,
                            timestamp=datetime.now(),
                        )
                    )
                    last_content = full_content
                    error_msg = None
                    error_detail = None

                except TimeoutError:
                    error_msg = f"LLM 流式调用超时（{self._harness_settings.llm_stream_timeout}s）"
                    error_detail = ExecutionError(
                        code="llm_timeout",
                        message=error_msg,
                        error_type="TimeoutError",
                        retryable=True,
                        phase="llm_call",
                    )
                    logger.warning("llm_stream_timeout", run_id=rid, error=error_msg)
                    await self._emit(
                        EventType.RUN_FAILED,
                        {
                            "call_id": llm_call_id,
                            "error": error_msg,
                            "phase": "llm_call",
                        },
                        session_id,
                        rid,
                    )
                    await self._emit_llm_call_end(
                        llm_call_id, status="failed", session_id=session_id, run_id=rid
                    )
                    budget.increment_retry()
                    continue
                except Exception as e:
                    logger.error("llm_call_failed", error=str(e), run_id=rid)
                    error_msg = f"LLM 调用失败: {e}"
                    error_detail = ExecutionError.from_exception(
                        e,
                        code="llm_call_failed",
                        retryable=not isinstance(e, BudgetExceeded),
                        phase="llm_call",
                        stack=traceback.format_exc(),
                    )
                    await self._emit(
                        EventType.RUN_FAILED,
                        {
                            "call_id": llm_call_id,
                            "error": str(e) or type(e).__name__,
                            "error_detail": error_detail.model_dump(mode="json"),
                            "phase": "llm_call",
                        },
                        session_id,
                        rid,
                    )
                    await self._emit_llm_call_end(
                        llm_call_id, status="failed", session_id=session_id, run_id=rid
                    )
                    budget.increment_retry()
                    if not isinstance(e, BudgetExceeded):
                        continue
                    raise

                duration_ms = (time.time() - start_time) * 1000

                await self._emit_llm_call_end(
                    llm_call_id,
                    status="completed",
                    session_id=session_id,
                    run_id=rid,
                    duration_ms=duration_ms,
                    tool_calls_count=len(final_tc),
                    tool_calls=final_tc,
                )

                # 持久化 assistant 消息（中断恢复关键）
                # 守卫：仅在确实有输出（文本或工具调用）时落库，避免空消息污染对话
                if full_content.strip() or final_tc:
                    await self._db.messages.save(
                        Message(
                            id=generate_time_id(),
                            session_id=session_id,
                            role=MessageRole.ASSISTANT,
                            content=full_content,
                            tool_calls=final_tc,
                            run_id=rid,
                            timestamp=datetime.now(),
                        )
                    )

                # 没有工具调用 → 终止循环
                if not final_tc:
                    break

                # 工具执行（并行）；调用 ID 在事件和工具账本中保持稳定。
                await self._emit_thinking(
                    EventType.THINKING_SUMMARY,
                    "正在执行工具",
                    session_id,
                    rid,
                )
                tool_calls: list[tuple[str, dict[str, Any]]] = []
                for tc in final_tc:
                    tool_calls.append((generate_time_id(), tc))

                tool_messages = await self._execute_tool_calls(
                    tool_calls=tool_calls,
                    session_id=session_id,
                    run_id=rid,
                    turn_count=budget.turn_count,
                    tool_results_all=tool_results_all,
                )

                for tm in tool_messages:
                    domain_messages.append(
                        Message(
                            id=generate_time_id(),
                            session_id=session_id,
                            role=MessageRole.TOOL,
                            content=str(tm.content),
                            tool_call_id=tm.tool_call_id,
                            run_id=rid,
                            timestamp=datetime.now(),
                        )
                    )

                # 工具调用成功 → 重置重试计数
                budget.reset_retries()

                if self._should_stop():
                    interrupted = True
                    break

        except BudgetExceeded as e:
            error_msg = str(e) or type(e).__name__
            error_detail = ExecutionError.from_exception(
                e, code="budget_exceeded", retryable=False, phase="harness"
            )
            logger.warning("budget_exceeded", run_id=rid, error=error_msg)
            await self._emit(
                EventType.BUDGET_EXCEEDED,
                {"reason": error_msg, "turn_count": budget.turn_count},
                session_id,
                rid,
            )
        except Exception as e:
            error_msg = (
                f"Harness 执行异常: {e}"
                if str(e)
                else f"Harness 执行异常: {type(e).__name__}"
            )
            error_detail = ExecutionError.from_exception(
                e,
                code="harness_failed",
                retryable=False,
                phase="harness",
                stack=traceback.format_exc(),
            )
            logger.exception("harness_failed", run_id=rid)
            await self._emit(
                EventType.RUN_FAILED,
                {
                    "error": error_msg,
                    "error_detail": error_detail.model_dump(mode="json"),
                    "phase": "harness",
                },
                session_id,
                rid,
            )

        # 停止信号触发的提前退出：统一标记 interrupted（覆盖 while 顶部退出路径）
        if self._should_stop():
            interrupted = True

        # 后处理
        if self._answer_stream is not None:
            await self._answer_stream.flush(is_complete=True)
        await self._emit_thinking(
            EventType.THINKING_COMPLETED,
            "",
            session_id,
            rid,
        )
        await self._emit(
            EventType.STREAM_END,
            {
                "run_id": rid,
                "stream_id": self._answer_stream_id,
                "stream_type": "answer",
                "turn_count": budget.turn_count,
                "content": full_content,
                "error": error_msg,
            },
            session_id,
            rid,
        )

        if parent_run_id is None:
            await self._db.sessions.update(
                session_id, status="idle" if not interrupted else "interrupted"
            )

        return HarnessRunResult(
            content=last_content,
            run_id=rid,
            turn_count=budget.turn_count,
            tool_results=tool_results_all,
            error=error_msg,
            error_detail=error_detail.model_dump(mode="json") if error_detail else None,
            interrupted=interrupted,
        )

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    async def _execute_tool_calls(
        self,
        tool_calls: list[tuple[str, dict[str, Any]]],
        session_id: str,
        run_id: str,
        tool_results_all: list[dict[str, Any]],
        turn_count: int = 0,
    ) -> list[ToolMessage]:
        """并行执行所有工具调用，同时记录日志与推送事件.

        使用 asyncio.gather 并行执行，每个工具调用独立创建 ToolCallRecord。

        参数：
            tool_calls: 预分配的工具调用计划，每项为 (call_id, tool_call_dict)。
            session_id: 会话 ID。
            run_id: 运行 ID。
            turn_count: 当前 LLM 轮次，用于工具记录的步骤编号。
            tool_results_all: 累积工具结果的列表（原地追加）。

        返回值：
            ToolMessage 列表，按输入顺序排列，用于追加到 LLM 消息上下文。
        """

        async def _execute_one(call_id: str, tc: dict[str, Any]) -> ToolMessage:
            """执行一个工具调用，并只向模型返回最终尝试的结果。"""
            tool_name = tc.get("name", "")
            args = tc.get("args", {}) or {}
            tc_id = tc.get("id", generate_time_id())

            def append_result(item: dict[str, Any]) -> None:
                # 同一 checkpoint 重放只保留一个对外结果；账本中的每次尝试
                # 仍然独立保存，便于定位审批和参数变化。
                if item.get("tool_call_id") and any(
                    existing.get("tool_call_id") == item["tool_call_id"]
                    for existing in tool_results_all
                ):
                    return
                tool_results_all.append(item)

            async def ensure_tool_message(content: str, record_id: str) -> None:
                message_id = self._tool_message_id(run_id, record_id)
                get_message = getattr(self._db.messages, "get", None)
                if (
                    get_message is not None
                    and await get_message(message_id) is not None
                ):
                    return
                await self._db.messages.save(
                    Message(
                        id=message_id,
                        session_id=session_id,
                        role=MessageRole.TOOL,
                        content=content,
                        tool_call_id=tc_id,
                        run_id=run_id,
                        tool_call_record_id=record_id,
                        tool_name=tool_name,
                        timestamp=datetime.now(),
                    )
                )

            try:
                (
                    result_content,
                    status,
                    error_msg,
                    error_stack,
                    record_id,
                    duration_ms,
                ) = await self._execute_single_tool(
                    tool_name=tool_name,
                    args=args,
                    session_id=session_id,
                    run_id=run_id,
                    call_id=call_id,
                    tool_call_id=tc_id,
                    turn_count=turn_count,
                )
                append_result(
                    {
                        "tool_call_id": tc_id,
                        "tool_name": tool_name,
                        "arguments": args,
                        "output": result_content,
                        "status": status,
                        "duration_ms": duration_ms,
                        "error": error_msg,
                    }
                )

                # 失败时把错误详情回传给 LLM，使其能据此自愈；checkpoint 重放
                # 已完成失败尝试时也只复用结果，不重新触发下一次执行。
                tool_content = result_content
                if status not in {"success", "denied", "unknown"} and error_msg:
                    tool_content = f"[工具 {tool_name} 失败]: {error_msg}"
                await ensure_tool_message(tool_content, record_id)
                return ToolMessage(content=tool_content, tool_call_id=tc_id)
            except Exception as e:
                # 保留原始 tc_id 以便 LLM 关联 tool_call → tool_message。
                logger.error("tool_execution_failed", tool=tool_name, error=str(e))
                append_result(
                    {
                        "tool_call_id": tc_id,
                        "tool_name": tool_name,
                        "arguments": args,
                        "output": "",
                        "status": "failed",
                        "duration_ms": 0,
                        "error": str(e) or type(e).__name__,
                    }
                )
                return ToolMessage(
                    content=f"[工具执行异常] {e}",
                    tool_call_id=tc_id,
                )

        # 并行执行
        tasks = [_execute_one(call_id, tc) for call_id, tc in tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        tool_messages: list[ToolMessage] = []
        for r in results:
            if isinstance(r, BaseException):
                # 理论上不应到达这里（_execute_one 已内部捕获），
                # 但保留防御性处理以防 gather 本身出错（如 CancelledError）
                logger.error("tool_task_failed", error=str(r))
                tool_messages.append(
                    ToolMessage(
                        content=f"[工具任务异常] {r}",
                        tool_call_id=generate_time_id(),
                    )
                )
            elif isinstance(r, ToolMessage):
                tool_messages.append(r)
        return tool_messages

    async def _execute_single_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        session_id: str,
        run_id: str,
        call_id: str = "tool-call",
        tool_call_id: str = "",
        turn_count: int = 0,
    ) -> tuple[str, str, str | None, str | None, str, float]:
        """执行工具尝试并为每次重试建立独立审批和账本记录。

        自愈流程:
        1. 白名单校验（子 Agent 防御性拒绝）
        2. 执行工具（带超时）
        3. 失败时查 Fallback 路由 → retry / alternate / passthrough / escalate
        4. 熔断器打开时直接返回失败

        参数：
            tool_name: 工具名称。
            args: 工具调用参数。
            session_id: 会话 ID。
            run_id: 运行 ID。
            call_id: LLM 生成的稳定调用批次 ID。
            tool_call_id: LLM 原始工具调用 ID；所有重试共享该 ID。
            turn_count: 当前 LLM 轮次。

        返回值：
            六元组 ``(result_content, status, error_msg, error_stack, record_id, duration_ms)``:
            - result_content: 工具输出文本（成功）或错误描述（失败）。
            - status: "success" / "failed" / "denied" / "unknown"。
            - error_msg: 错误信息（成功时为 None）。
            - error_stack: 错误堆栈（成功时为 None）。
            - record_id: 最终尝试的账本 ID。
            - duration_ms: 最终尝试的实际工具执行耗时，不包含审批等待。
        """
        # 子 Agent 工具白名单校验：白名单外的工具一律拒绝执行（防御模型误调）
        if (
            self._allowed_tool_names is not None
            and tool_name not in self._allowed_tool_names
        ):
            record_id = self._tool_record_id(
                run_id=run_id,
                call_id=call_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args=args,
                attempt_number=1,
            )
            return (
                f"[工具 {tool_name} 不在当前 Agent 的允许列表]",
                "failed",
                f"Tool '{tool_name}' is not allowed for this agent",
                None,
                record_id,
                0,
            )

        async def execute_attempt(
            current_tool_name: str,
            current_params: dict[str, Any],
            attempt_number: int,
        ) -> tuple[str, str, str | None, str | None, str, float, bool]:
            """准备、领取并执行一次工具尝试；最后一个布尔值表示是否重放。"""
            record_id = self._tool_record_id(
                run_id=run_id,
                call_id=call_id,
                tool_call_id=tool_call_id,
                tool_name=current_tool_name,
                args=current_params,
                attempt_number=attempt_number,
            )
            get_record = getattr(self._db.tool_calls, "get", None)
            record = await get_record(record_id) if get_record is not None else None

            if record is not None:
                if record.status == ToolCallStatus.SUCCESS:
                    return (
                        record.raw_output or "",
                        "success",
                        None,
                        None,
                        record_id,
                        record.duration_ms,
                        True,
                    )
                if record.status in {
                    ToolCallStatus.FAILED,
                    ToolCallStatus.DENIED,
                    ToolCallStatus.TIMEOUT,
                }:
                    error = record.error_message or "工具调用已完成但失败"
                    output = record.raw_output or ""
                    return (
                        output or f"[工具 {current_tool_name} 失败]: {error}",
                        record.status.value,
                        None if record.status == ToolCallStatus.DENIED else error,
                        record.error_stack,
                        record_id,
                        record.duration_ms,
                        True,
                    )
                if record.status in {ToolCallStatus.UNKNOWN, ToolCallStatus.RUNNING}:
                    return (
                        f"[工具 {current_tool_name} 状态未确认，未自动执行]",
                        "unknown",
                        "工具调用状态未确认",
                        None,
                        record_id,
                        record.duration_ms,
                        True,
                    )
            else:
                await self._db.tool_calls.save(
                    ToolCallRecord(
                        id=record_id,
                        session_id=session_id,
                        step_id=record_id,
                        run_id=run_id,
                        attempt_number=attempt_number,
                        tool_name=current_tool_name,
                        arguments=dict(current_params),
                        status=ToolCallStatus.PENDING,
                        started_at=datetime.now(),
                    )
                )

            execution_started_at: float | None = None

            async def on_approval_created(new_approval_id: str) -> None:
                """把本次尝试的审批 ID 写回账本。"""
                await self._db.tool_calls.update(
                    record_id, {"approval_id": new_approval_id}
                )

            async def on_execution_start() -> bool:
                """原子领取 PENDING 账本并发布真实执行开始事件。"""
                nonlocal execution_started_at
                claim = getattr(self._db.tool_calls, "claim_for_execution", None)
                if claim is not None:
                    claimed = await claim(record_id)
                else:
                    await self._db.tool_calls.update(
                        record_id, {"status": ToolCallStatus.RUNNING.value}
                    )
                    claimed = True
                if not claimed:
                    return False
                execution_started_at = time.monotonic()
                await self._emit(
                    EventType.TOOL_CALL_START,
                    {
                        "tool_call_id": record_id,
                        "call_id": call_id,
                        "tool_name": current_tool_name,
                        "arguments": current_params,
                        "risk_level": self._tool_manager.get_risk_level(
                            current_tool_name
                        ),
                    },
                    session_id,
                    run_id,
                )
                return True

            approval_id = record.approval_id if record is not None else None
            try:
                result = await self._tool_manager.call_tool(
                    name=current_tool_name,
                    params=dict(current_params),
                    session_id=session_id,
                    run_id=run_id,
                    tool_call_id=record_id,
                    agent_role=self._agent_role,
                    plan_id=self._plan_id,
                    task_id=self._task_id,
                    worker_run_id=(run_id if self._agent_role == "worker" else None),
                    depth=self._depth,
                    execution_timeout=self._harness_settings.tool_timeout,
                    approval_id=approval_id,
                    on_approval_created=on_approval_created,
                    on_execution_start=on_execution_start,
                )
            except Exception as exc:
                duration_ms = (
                    (time.monotonic() - execution_started_at) * 1000
                    if execution_started_at is not None
                    else 0
                )
                await self._db.tool_calls.update(
                    record_id,
                    {
                        "status": ToolCallStatus.FAILED.value,
                        "completed_at": datetime.now().isoformat(),
                        "duration_ms": duration_ms,
                        "error_message": str(exc) or type(exc).__name__,
                        "error_stack": traceback.format_exc(),
                    },
                )
                await self._emit_tool_call_end(
                    record_id,
                    call_id,
                    current_tool_name,
                    "failed",
                    None,
                    str(exc) or type(exc).__name__,
                    duration_ms,
                    session_id,
                    run_id,
                )
                raise

            duration_ms = (
                (time.monotonic() - execution_started_at) * 1000
                if execution_started_at is not None
                else 0
            )
            output = result.output or ""
            result_status = result.status
            if result_status == "success":
                status = ToolCallStatus.SUCCESS.value
                error_message = None
                raw_output = output
            elif result_status == "denied":
                status = ToolCallStatus.DENIED.value
                error_message = None
                raw_output = None
            elif result_status == "unknown":
                # 竞争失败时不要覆盖另一进程写入的 unknown/running 状态。
                return (
                    output or "工具调用状态未确认",
                    "unknown",
                    result.error or "工具调用状态未确认",
                    None,
                    record_id,
                    duration_ms,
                    False,
                )
            else:
                status = ToolCallStatus.FAILED.value
                error_message = result.error or f"工具 {current_tool_name} 执行失败"
                raw_output = None

            await self._db.tool_calls.update(
                record_id,
                {
                    "raw_output": raw_output,
                    "status": status,
                    "completed_at": datetime.now().isoformat(),
                    "duration_ms": duration_ms,
                    "error_message": error_message,
                    "error_stack": None,
                },
            )
            await self._emit_tool_call_end(
                record_id,
                call_id,
                current_tool_name,
                status,
                output if status == ToolCallStatus.SUCCESS.value else None,
                error_message,
                duration_ms,
                session_id,
                run_id,
            )
            return (
                output,
                status,
                error_message,
                None,
                record_id,
                duration_ms,
                False,
            )

        last_error: Exception | None = None
        last_stack: str | None = None
        last_record_id = ""
        last_duration_ms = 0.0
        params = dict(args)
        attempt_number = 1
        retry_count = 0
        current_tool_name = tool_name
        while True:
            try:
                (
                    output,
                    status,
                    error_message,
                    error_stack,
                    record_id,
                    duration_ms,
                    replayed,
                ) = await execute_attempt(current_tool_name, params, attempt_number)
                last_record_id = record_id
                last_duration_ms = duration_ms
                if replayed:
                    return (
                        output,
                        status,
                        error_message,
                        error_stack,
                        record_id,
                        duration_ms,
                    )
                if status in {
                    ToolCallStatus.SUCCESS.value,
                    ToolCallStatus.DENIED.value,
                    ToolCallStatus.UNKNOWN.value,
                }:
                    if status == ToolCallStatus.SUCCESS.value:
                        self._error_handler.record_result(
                            current_tool_name, success=True
                        )
                    return (
                        output,
                        status,
                        error_message,
                        error_stack,
                        record_id,
                        duration_ms,
                    )
                raise RuntimeError(
                    error_message or f"工具 {current_tool_name} 执行失败"
                )
            except Exception as e:
                last_error = e
                last_stack = traceback.format_exc()
                self._error_handler.record_result(current_tool_name, success=False)

                # 熔断器三态: CLOSED → OPEN（失败率达阈值）→ HALF_OPEN（超时后探测）
                if self._error_handler.is_circuit_open(current_tool_name):
                    break
                fallback = self._error_handler.get_fallback(current_tool_name, e)
                if fallback is None:
                    break

                action = fallback.action
                if action == "retry" and retry_count < fallback.max_retries:
                    if fallback.param_transform:
                        params = self._error_handler.apply_param_transform(
                            params, fallback
                        )
                    retry_count += 1
                    attempt_number += 1
                    logger.info(
                        "tool_retry_with_fallback",
                        tool=current_tool_name,
                        attempt=attempt_number,
                    )
                    continue
                if action == "alternate" and fallback.alternate_tool:
                    attempt_number += 1
                    try:
                        (
                            alt_output,
                            alt_status,
                            alt_error,
                            alt_stack,
                            alt_record_id,
                            alt_duration,
                            alt_replayed,
                        ) = await execute_attempt(
                            fallback.alternate_tool, params, attempt_number
                        )
                        last_record_id = alt_record_id
                        last_duration_ms = alt_duration
                        if alt_status == ToolCallStatus.SUCCESS.value:
                            if not alt_replayed:
                                self._error_handler.record_result(
                                    fallback.alternate_tool, success=True
                                )
                            return (
                                alt_output,
                                alt_status,
                                alt_error,
                                alt_stack,
                                alt_record_id,
                                alt_duration,
                            )
                        if alt_status in {
                            ToolCallStatus.DENIED.value,
                            ToolCallStatus.UNKNOWN.value,
                        }:
                            return (
                                alt_output,
                                alt_status,
                                alt_error,
                                alt_stack,
                                alt_record_id,
                                alt_duration,
                            )
                        last_error = RuntimeError(
                            alt_error or f"备用工具 {fallback.alternate_tool} 执行失败"
                        )
                        last_stack = alt_stack
                    except Exception as alt_err:
                        self._error_handler.record_result(
                            fallback.alternate_tool, success=False
                        )
                        last_error = alt_err
                        last_stack = traceback.format_exc()
                    break
                if action == "passthrough":
                    return (
                        f"[工具 {tool_name} 返回空结果]",
                        ToolCallStatus.SUCCESS.value,
                        None,
                        None,
                        last_record_id,
                        last_duration_ms,
                    )
                break

        err_msg = str(last_error) if last_error else "未知错误"
        return (
            f"[工具 {tool_name} 执行失败]",
            ToolCallStatus.FAILED.value,
            err_msg,
            last_stack,
            last_record_id
            or self._tool_record_id(
                run_id=run_id,
                call_id=call_id,
                tool_call_id=tool_call_id,
                tool_name=current_tool_name,
                args=params,
                attempt_number=attempt_number,
            ),
            last_duration_ms,
        )

    async def _emit_tool_call_end(
        self,
        record_id: str,
        call_id: str,
        tool_name: str,
        status: str,
        output: str | None,
        error: str | None,
        duration_ms: float,
        session_id: str,
        run_id: str,
    ) -> None:
        """发布一次工具尝试的完成事件。

        参数:
            record_id (str): 工具尝试账本 ID。
            call_id (str): LLM 工具调用批次 ID。
            tool_name (str): 实际执行的工具名称。
            status (str): 工具尝试终态。
            output (str | None): 成功时的工具输出。
            error (str | None): 失败原因。
            duration_ms (float): 实际执行耗时，不含审批等待。
            session_id (str): 会话 ID。
            run_id (str): 运行 ID。
        返回值:
            None。
        异常:
            事件发布失败时由 ``_emit`` 记录并吞掉异常。
        """
        await self._emit(
            EventType.TOOL_CALL_END,
            {
                "tool_call_id": record_id,
                "call_id": call_id,
                "tool_name": tool_name,
                "status": status,
                "output": output,
                "error": error,
                "error_detail": (
                    ExecutionError.from_legacy_value(
                        error,
                        code=(
                            "tool_timeout"
                            if status == ToolCallStatus.TIMEOUT.value
                            else "tool_failed"
                        ),
                        retryable=status == ToolCallStatus.TIMEOUT.value,
                        phase="tool_execution",
                    ).model_dump(mode="json")
                    if error
                    else None
                ),
                "duration_ms": duration_ms,
            },
            session_id,
            run_id,
        )

    def _assemble_response(
        self,
        stream_chunks: list[AIMessageChunk],
        fallback_content: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        """合并流式 chunk，提取完整 content 与 tool_calls.

        利用 langchain 的 AIMessageChunk.__add__ 拼接文本分片并累加 tool_call
        的 args JSON 分片，比手动拼装可靠。空流时回退到已累积的 fallback_content。

        参数：
            stream_chunks: LLM 流式返回的 AIMessageChunk 列表。
            fallback_content: 流为空时的回退文本（来自已累积的 full_content）。

        返回值：
            二元组 ``(content, tool_calls)``:
            - content: 合并后的完整文本内容。
            - tool_calls: 归一化后的工具调用列表，每项含 id / name / args。
        """
        if not stream_chunks:
            return fallback_content, []
        merged = stream_chunks[0]
        for c in stream_chunks[1:]:
            merged = merged + c
        content = extract_message_text(merged) or fallback_content
        raw_tcs = getattr(merged, "tool_calls", None) or []
        return content, self._normalize_tool_calls(raw_tcs)

    @staticmethod
    def _normalize_tool_calls(
        tool_calls: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """将 langchain tool_calls 归一化为统一的内部格式."""
        return normalize_tool_calls(tool_calls)

    def _tool_record_id(
        self,
        *,
        run_id: str,
        call_id: str,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, Any],
        attempt_number: int = 1,
    ) -> str:
        """返回带尝试序号的工具账本 ID。

        同一 LLM tool call 的第一次尝试沿用旧的稳定 ID，后续尝试追加序号；
        这样既兼容已有 checkpoint，又保证每次重试都有独立账本和审批。
        """

        if not self._stable_tool_ids:
            return generate_time_id()
        base = f"{run_id}:tool-record:{call_id}:{tool_call_id}"
        return base if attempt_number == 1 else f"{base}:attempt:{attempt_number}"

    def _tool_message_id(self, run_id: str, tool_record_id: str) -> str:
        """返回工具消息 ID；逐轮执行器恢复时可安全重放。"""

        if not self._stable_tool_ids:
            return generate_time_id()
        return f"{run_id}:tool-message:{tool_record_id}"

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _should_stop(self) -> bool:
        """检查是否应停止运行.

        返回值：
            True 表示外部 stop_signal 已置位。
        """
        return self._stop_signal is not None and self._stop_signal.is_set()

    @staticmethod
    def _message_id_from_messages(
        messages: Sequence[Message | dict[str, Any]],
    ) -> str | None:
        """从本次 Harness 输入中提取最新用户消息 ID。"""
        for message in reversed(messages):
            if isinstance(message, Message):
                if message.role == MessageRole.USER:
                    return message.id
                continue
            if message.get("role") != MessageRole.USER.value:
                continue
            message_id = message.get("id")
            if message_id:
                return str(message_id)
            metadata = message.get("metadata") or {}
            if metadata.get("message_id"):
                return str(metadata["message_id"])
        return None

    async def _emit(
        self,
        event_type: EventType,
        data: dict[str, Any],
        session_id: str,
        run_id: str,
    ) -> None:
        """发布 Runtime 应用事件。

        推送异常仅记录警告日志，不向上抛出。

        参数：
            event_type: 事件类型枚举。
            data: 事件数据字典。
            session_id: 会话 ID。
            run_id: 运行 ID。
        """
        try:
            data = dict(data)
            if self._message_id is not None:
                data.setdefault("message_id", self._message_id)
            if event_type == EventType.LLM_TOKEN:
                if self._answer_stream is not None:
                    await self._answer_stream.append(
                        str(data.get("token", data.get("delta", "")))
                    )
                return
            durability = (
                EventDurability.REALTIME
                if event_type in {EventType.LLM_TOKEN}
                else EventDurability.DURABLE
            )
            await self._event_publisher.publish(
                ApplicationEvent(
                    event_type=event_type,
                    durability=durability,
                    session_id=session_id,
                    run_id=run_id,
                    message_id=self._message_id,
                    stream_id=str(data["stream_id"]) if data.get("stream_id") else None,
                    stream_type=(
                        str(data["stream_type"]) if data.get("stream_type") else None
                    ),
                    parent_run_id=self._parent_run_id,
                    transition_id=(
                        str(data["transition_id"])
                        if data.get("transition_id")
                        else self._event_transition_id(event_type, data)
                    ),
                    payload=data,
                )
            )
        except Exception as e:
            logger.warning(
                "emit_event_failed", event_type=str(event_type), error=str(e)
            )

    async def _emit_thinking(
        self,
        event_type: EventType,
        content: str,
        session_id: str,
        run_id: str,
    ) -> None:
        """发布受控的思考阶段事件，不暴露模型隐藏推理内容。

        thinking 只依赖 durable event 的会话序号来重放；它不属于可恢复的
        Agent 执行状态，也不维护流快照。
        """
        stream_id = f"thinking-{run_id}"
        try:
            await self._event_publisher.publish(
                ApplicationEvent(
                    event_type=event_type,
                    durability=EventDurability.DURABLE,
                    session_id=session_id,
                    run_id=run_id,
                    message_id=self._message_id,
                    stream_id=stream_id,
                    stream_type="thinking",
                    chunk_id=None,
                    transition_id=(
                        f"thinking:{run_id}:{event_type.value}"
                        if event_type
                        in {EventType.THINKING_STARTED, EventType.THINKING_COMPLETED}
                        else None
                    ),
                    payload={"content": content, "stream_id": stream_id},
                )
            )
        except Exception as exc:
            logger.warning(
                "emit_thinking_failed", event_type=str(event_type), error=str(exc)
            )

    @staticmethod
    def _event_transition_id(event_type: EventType, data: dict[str, Any]) -> str | None:
        """Derive stable IDs for low-frequency lifecycle events."""
        if event_type in {EventType.LLM_CALL_START, EventType.LLM_CALL_END}:
            call_id = data.get("call_id")
            return f"llm:{call_id}:{event_type.value}" if call_id else None
        if event_type in {EventType.STREAM_START, EventType.STREAM_END}:
            stream_id = data.get("stream_id")
            return f"stream:{stream_id}:{event_type.value}" if stream_id else None
        if event_type in {EventType.TOOL_CALL_START, EventType.TOOL_CALL_END}:
            call_id = data.get("tool_call_id")
            return f"tool:{call_id}:{event_type.value}" if call_id else None
        return None

    async def _emit_llm_call_end(
        self,
        call_id: str,
        status: str,
        session_id: str,
        run_id: str,
        duration_ms: float = 0,
        tool_calls_count: int = 0,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """推送 LLM 调用结束事件.

        前端据此结束流式气泡: status=failed 时移除本次调用创建的无内容
        building 气泡，避免空响应/异常重试路径在界面上残留空气泡。
        成功路径与失败路径都必须调用，保证事件生命周期成对。
        纯工具调用回合（无文本）通过 tool_calls 把工具名/参数带给前端，
        使其与历史视图一致地展示该回合的工具卡片。

        参数：
            call_id: LLM 调用 ID。
            status: "completed" 或 "failed"。
            session_id: 会话 ID。
            run_id: 运行 ID。
            duration_ms: 调用耗时（毫秒）。
            tool_calls_count: 工具调用数量。
            tool_calls: 工具调用详情列表（含 name / args），用于前端展示。
        """
        await self._emit(
            EventType.LLM_CALL_END,
            {
                "call_id": call_id,
                "status": status,
                "duration_ms": duration_ms,
                "tool_calls_count": tool_calls_count,
                "tool_calls": tool_calls or [],
            },
            session_id,
            run_id,
        )
