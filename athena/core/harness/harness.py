"""Harness 执行引擎 — Agent 运行的核心编排器.

核心职责:
- 执行 LLM 调用（流式输出，支持超时兜底）
- 编排工具调用（asyncio.gather 并行执行无依赖工具）
- 预算控制（max_turns + retry_budget 双重限制）
- 事件流式推送到前端（WebSocket）
- 执行过程日志持久化到 SQLite（Step + ToolCallRecord）

执行流程:
    用户消息 → LLM 流式调用 → 工具调用（并行） → LLM 再调用 → ... → 终止

project_memory 约束:
- step_number 在 run_id 范围内全局递增，并行工具调用前预分配序号（避免 race condition）
- 一次 LLM 调用 = 1 step，一次工具执行 = 1 step
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
    AIMessage,
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
from athena.core.llm.tokens import token_usage_from_chunks
from athena.core.tools.manager import UnifiedToolManager
from athena.infrastructure.sqlite.database import Database
from athena.gateway.ws.manager import WebSocketManager
from athena.models import Message, MessageRole, Step, ToolCallRecord
from athena.models.step import StepStatus, StepType
from athena.models.tool import ToolCallStatus
from athena.gateway.ws.events import EventType, build_event
from athena.utils.ids import generate_time_id
from athena.utils.llm import extract_message_text
from athena.utils.logging import get_logger
from athena.utils.message import dict_to_message, normalize_tool_calls

logger = get_logger(__name__)


@dataclass
class HarnessSettings:
    """Harness 运行时配置.

    Attributes:
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

    Attributes:
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
    interrupted: bool = False


class Harness:
    """Agent 执行引擎 — 编排 LLM 调用与工具执行的核心循环.

    典型用法::

        harness = Harness(
            llm=provider,
            tool_manager=manager,
            settings=settings,
            db=db,
            ws_manager=ws_manager,
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
        ws_manager: WebSocketManager,
        compressor: ContextCompressor,
        error_handler: ToolErrorHandler | None = None,
        harness_settings: HarnessSettings | None = None,
    ) -> None:
        """初始化 Harness 实例.

        Args:
            llm: LLM Provider，负责模型调用（流式/非流式）。
            tool_manager: 统一工具管理器，负责工具注册与调用。
            settings: 全局配置。
            db: 数据库实例，用于持久化 Step / Message / ToolCallRecord。
            ws_manager: WebSocket 管理器，用于向前端推送事件。
            compressor: 上下文压缩器，在每轮 LLM 调用前压缩消息列表。
            error_handler: 工具错误自愈路由器（含熔断器）。未提供时使用默认实例。
            harness_settings: Harness 运行时配置。未提供时从全局 Settings 派生。
        """
        self._llm = llm
        self._tool_manager = tool_manager
        self._settings = settings
        self._db = db
        self._ws = ws_manager
        self._compressor = compressor
        self._error_handler = error_handler or ToolErrorHandler()
        self._harness_settings = harness_settings or HarnessSettings(
            max_turns_per_run=self._settings.max_turns_per_run,
            retry_budget=self._settings.retry_budget,
            tool_timeout=self._settings.tool_timeout,
            llm_stream_timeout=self._settings.llm_stream_timeout,
        )
        self._stop_event = asyncio.Event()
        self._stop_signal: asyncio.Event | None = None
        self._allowed_tool_names: set[str] | None = None
        self._parent_run_id: str | None = None
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
    ) -> HarnessRunResult:
        """执行单次 Agent 运行.

        主循环流程: LLM 流式调用 → 工具并行执行 → LLM 再调用 → ...
        终止条件: LLM 无工具调用 / 预算超限 / 停止信号 / 异常。

        Args:
            messages: 对话历史（含最新用户消息）。支持 Message 对象或 dict 格式。
            session_id: 会话 ID，用于关联 Step / Message / 事件推送。
            system_prompt: 系统提示词，作为第一条 SystemMessage 注入。
            run_id: 可选 run_id（子 Agent 使用），未提供则自动生成。
            parent_run_id: 父 run_id（子 Agent 运行时指向其父 run；主 run 为 None），
                写入每个 step，使步骤能显式追溯所属的任务树。
            tool_names: 可选工具白名单（子 Agent 使用），None 表示允许全部工具。
            stop_signal: 外部停止信号（会话级 stop 事件），由 gateway 层注入；
                与 request_stop() 的 _stop_event 等价，任一置位即终止运行。

        Returns:
            HarnessRunResult，包含最终文本、run_id、轮次、工具结果、错误和中断状态。

        Raises:
            BudgetExceeded: 预算超限且无剩余重试次数时抛出。
            Exception: 未预期的执行异常（会被捕获并记录到 error 字段）。
        """
        rid = run_id or generate_time_id()
        self._parent_run_id = parent_run_id
        budget = Budget(
            max_turns=self._harness_settings.max_turns_per_run,
            retry_budget=self._harness_settings.retry_budget,
        )
        self._stop_event.clear()
        self._stop_signal = stop_signal

        # 更新会话状态为 running
        await self._db.sessions.update(session_id, status="running", run_id=rid)

        await self._emit(EventType.STREAM_START, {"run_id": rid}, session_id, rid)

        # 构建消息列表
        lc_messages: list[BaseMessage] = []
        if system_prompt:
            lc_messages.append(SystemMessage(content=system_prompt))
        for m in messages:
            lc_messages.append(dict_to_message(m))

        # 绑定工具（子 Agent 按 tool_names 白名单过滤，None = 全部）
        self._allowed_tool_names = set(tool_names) if tool_names else None
        lc_tools = self._tool_manager.get_langchain_tools(names=tool_names)
        bound_llm = self._llm.bind_tools(lc_tools) if lc_tools else self._llm

        step_counter = 0
        tool_results_all: list[dict[str, Any]] = []
        last_content = ""
        error_msg: str | None = None
        interrupted = False

        try:
            while not self._should_stop():
                # 上下文压缩
                compressed = await self._compressor.compress(
                    lc_messages,
                    session_id=session_id,
                )
                if len(compressed) < len(lc_messages):
                    lc_messages = compressed

                # Step: LLM 调用
                step_counter += 1
                llm_step_id = generate_time_id()
                budget.increment_turn()

                await self._save_step(
                    Step(
                        id=llm_step_id,
                        session_id=session_id,
                        run_id=rid,
                        step_number=step_counter,
                        step_type=StepType.LLM_CALL,
                        status=StepStatus.RUNNING,
                        started_at=datetime.now(),
                    )
                )

                await self._emit(
                    EventType.LLM_CALL_START,
                    {"step_id": llm_step_id, "turn": budget.turn_count},
                    session_id,
                    rid,
                )

                start_time = time.time()
                full_content = ""
                stream_chunks: list[AIMessageChunk] = []
                try:
                    # 流式调用整体包超时兜底：流挂死时 stop 的 _should_stop() break
                    # 永远等不到 __anext__，只有超时能终态化当前 LLM step
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
                                await self._emit(
                                    EventType.LLM_TOKEN,
                                    {"token": chunk_content},
                                    session_id,
                                    rid,
                                )

                        # 合并流式 chunk → 完整响应（content + tool_calls）
                        merged_content, final_tc = self._assemble_response(
                            stream_chunks, full_content
                        )
                        token_usage = token_usage_from_chunks(stream_chunks)
                    if merged_content:
                        full_content = merged_content

                    # 停止信号在流式中途置位 → 结束本轮 run
                    # （不落库残缺 assistant 消息、不执行残缺 tool_calls；
                    #   必须终态化当前 llm step 并补发 LLM_CALL_END，
                    #   否则 step 遗留 running、前端气泡不结束）
                    if self._should_stop():
                        interrupted = True
                        await self._update_step(
                            llm_step_id,
                            {
                                "status": str(StepStatus.FAILED),
                                "completed_at": datetime.now().isoformat(),
                                "duration_ms": (time.time() - start_time) * 1000,
                                "error_message": "运行被用户停止",
                            },
                        )
                        await self._emit_llm_call_end(
                            llm_step_id,
                            status="failed",
                            session_id=session_id,
                            run_id=rid,
                        )
                        break

                    # 空响应检测：无文本且无工具调用 → 不落库、有界重试
                    if not full_content.strip() and not final_tc:
                        error_msg = "LLM 返回空响应（无内容且无工具调用）"
                        logger.warning(
                            "llm_empty_response", run_id=rid, error=error_msg
                        )
                        await self._update_step(
                            llm_step_id,
                            {
                                "status": str(StepStatus.FAILED),
                                "completed_at": datetime.now().isoformat(),
                                "duration_ms": (time.time() - start_time) * 1000,
                                "error_message": error_msg,
                            },
                        )
                        await self._emit(
                            EventType.ERROR,
                            {
                                "step_id": llm_step_id,
                                "error": error_msg,
                                "phase": "llm_call",
                            },
                            session_id,
                            rid,
                        )
                        # 失败路径同样需结束 LLM 调用生命周期：前端据此移除
                        # 本次调用创建的流式气泡，避免重试残留空气泡
                        await self._emit_llm_call_end(
                            llm_step_id,
                            status="failed",
                            session_id=session_id,
                            run_id=rid,
                        )
                        if budget.remaining_retries() > 0:
                            budget.increment_retry()
                            continue
                        break

                    ai_message = (
                        AIMessage(content=full_content, tool_calls=final_tc)
                        if final_tc
                        else AIMessage(content=full_content)
                    )
                    lc_messages.append(ai_message)
                    last_content = full_content
                    error_msg = None

                except TimeoutError:
                    error_msg = f"LLM 流式调用超时（{self._harness_settings.llm_stream_timeout}s）"
                    logger.warning("llm_stream_timeout", run_id=rid, error=error_msg)
                    await self._update_step(
                        llm_step_id,
                        {
                            "status": str(StepStatus.FAILED),
                            "completed_at": datetime.now().isoformat(),
                            "duration_ms": (time.time() - start_time) * 1000,
                            "error_message": error_msg,
                        },
                    )
                    await self._emit(
                        EventType.ERROR,
                        {
                            "step_id": llm_step_id,
                            "error": error_msg,
                            "phase": "llm_call",
                        },
                        session_id,
                        rid,
                    )
                    await self._emit_llm_call_end(
                        llm_step_id, status="failed", session_id=session_id, run_id=rid
                    )
                    budget.increment_retry()
                    continue
                except Exception as e:
                    logger.error("llm_call_failed", error=str(e), run_id=rid)
                    error_msg = f"LLM 调用失败: {e}"
                    await self._update_step(
                        llm_step_id,
                        {
                            "status": str(StepStatus.FAILED),
                            "completed_at": datetime.now().isoformat(),
                            "duration_ms": (time.time() - start_time) * 1000,
                            "error_message": str(e),
                        },
                    )
                    await self._emit(
                        EventType.ERROR,
                        {"step_id": llm_step_id, "error": str(e), "phase": "llm_call"},
                        session_id,
                        rid,
                    )
                    await self._emit_llm_call_end(
                        llm_step_id, status="failed", session_id=session_id, run_id=rid
                    )
                    budget.increment_retry()
                    if not isinstance(e, BudgetExceeded):
                        continue
                    raise

                # 更新 LLM 步骤记录
                duration_ms = (time.time() - start_time) * 1000
                await self._update_step(
                    llm_step_id,
                    {
                        "status": str(StepStatus.COMPLETED),
                        "completed_at": datetime.now().isoformat(),
                        "duration_ms": duration_ms,
                        "llm_input_tokens": (
                            token_usage.input_tokens
                            if token_usage is not None
                            else bound_llm.count_message_tokens(lc_messages[:-1])
                        ),
                        "llm_output_tokens": (
                            token_usage.output_tokens
                            if token_usage is not None
                            else bound_llm.count_message_tokens([ai_message])
                        ),
                    },
                )

                await self._emit_llm_call_end(
                    llm_step_id,
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
                            step_id=llm_step_id,
                            timestamp=datetime.now(),
                        )
                    )

                # 没有工具调用 → 终止循环
                if not final_tc:
                    break

                # Step: 工具执行（并行）
                # 预分配 step_number（project_memory 约束：避免并行 race condition）
                tool_step_starts: list[tuple[int, str, dict[str, Any]]] = []
                for tc in final_tc:
                    step_counter += 1
                    tool_step_id = generate_time_id()
                    tool_step_starts.append((step_counter, tool_step_id, tc))

                tool_messages = await self._execute_tool_calls(
                    tool_step_starts=tool_step_starts,
                    parent_step_id=llm_step_id,
                    session_id=session_id,
                    run_id=rid,
                    tool_results_all=tool_results_all,
                )

                for tm in tool_messages:
                    lc_messages.append(tm)

                # 工具调用成功 → 重置重试计数
                budget.reset_retries()

                if self._should_stop():
                    interrupted = True
                    break

        except BudgetExceeded as e:
            error_msg = str(e)
            logger.warning("budget_exceeded", run_id=rid, error=error_msg)
            await self._emit(
                EventType.BUDGET_EXCEEDED,
                {"reason": error_msg, "turn_count": budget.turn_count},
                session_id,
                rid,
            )
        except Exception as e:
            error_msg = f"Harness 执行异常: {e}"
            logger.exception("harness_failed", run_id=rid)
            await self._emit(
                EventType.ERROR,
                {"error": str(e), "phase": "harness"},
                session_id,
                rid,
            )

        # 停止信号触发的提前退出：统一标记 interrupted（覆盖 while 顶部退出路径）
        if self._should_stop():
            interrupted = True

        # 后处理
        await self._emit(
            EventType.STREAM_END,
            {"run_id": rid, "turn_count": budget.turn_count, "error": error_msg},
            session_id,
            rid,
        )

        await self._db.sessions.update(
            session_id, status="idle" if not interrupted else "interrupted"
        )

        return HarnessRunResult(
            content=last_content,
            run_id=rid,
            turn_count=budget.turn_count,
            tool_results=tool_results_all,
            error=error_msg,
            interrupted=interrupted,
        )

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    async def _execute_tool_calls(
        self,
        tool_step_starts: list[tuple[int, str, dict[str, Any]]],
        parent_step_id: str,
        session_id: str,
        run_id: str,
        tool_results_all: list[dict[str, Any]],
    ) -> list[ToolMessage]:
        """并行执行所有工具调用，同时记录日志与推送事件.

        使用 asyncio.gather 并行执行，每个工具调用独立创建 Step 和
        ToolCallRecord 记录。单个工具的异常不会影响其他工具执行。

        Args:
            tool_step_starts: 预分配的工具执行计划，每项为
                (step_number, step_id, tool_call_dict)。
            parent_step_id: 父 LLM 调用的 step_id，用于建立步骤层级关系。
            session_id: 会话 ID。
            run_id: 运行 ID。
            tool_results_all: 累积工具结果的列表（原地追加）。

        Returns:
            ToolMessage 列表，按输入顺序排列，用于追加到 LLM 消息上下文。
        """

        async def _execute_one(
            step_number: int, step_id: str, tc: dict[str, Any]
        ) -> ToolMessage:
            """执行单个工具调用的完整生命周期: 创建记录 → 执行 → 更新状态 → 推送事件.

            异常会被内部捕获并转换为 ToolMessage 错误响应，确保不会中断
            asyncio.gather 中的其他工具执行。
            """
            tool_name = tc.get("name", "")
            args = tc.get("args", {}) or tc.get("arguments", {}) or {}
            tc_id = tc.get("id", generate_time_id())

            try:
                # 创建 tool_execution step
                await self._save_step(
                    Step(
                        id=step_id,
                        session_id=session_id,
                        run_id=run_id,
                        step_number=step_number,
                        step_type=StepType.TOOL_EXECUTION,
                        parent_step_id=parent_step_id,
                        status=StepStatus.RUNNING,
                        started_at=datetime.now(),
                    )
                )

                # 创建 tool_call 记录
                tc_record_id = generate_time_id()
                await self._db.tool_calls.save(
                    ToolCallRecord(
                        id=tc_record_id,
                        session_id=session_id,
                        step_id=step_id,
                        tool_name=tool_name,
                        arguments=args,
                        status=ToolCallStatus.RUNNING,
                        started_at=datetime.now(),
                    )
                )

                await self._emit(
                    EventType.TOOL_CALL_START,
                    {
                        "tool_call_id": tc_record_id,
                        "step_id": step_id,
                        "tool_name": tool_name,
                        "arguments": args,
                        "risk_level": self._tool_manager.get_risk_level(tool_name),
                    },
                    session_id,
                    run_id,
                )

                start_time = time.time()
                result_content, status, error_msg, error_stack = (
                    await self._execute_single_tool(
                        tool_name=tool_name,
                        args=args,
                        session_id=session_id,
                        run_id=run_id,
                        tool_call_id=tc_record_id,
                    )
                )
                duration_ms = (time.time() - start_time) * 1000

                # 更新 tool_call 记录
                await self._db.tool_calls.update(
                    tc_record_id,
                    {
                        "raw_output": (
                            result_content if status == "success" else None
                        ),
                        "status": status,
                        "completed_at": datetime.now().isoformat(),
                        "duration_ms": duration_ms,
                        "error_message": error_msg,
                        "error_stack": error_stack,
                    },
                )

                # 更新 tool_execution step
                await self._update_step(
                    step_id,
                    {
                        "status": (
                            str(StepStatus.COMPLETED)
                            if status == "success"
                            else str(StepStatus.FAILED)
                        ),
                        "completed_at": datetime.now().isoformat(),
                        "duration_ms": duration_ms,
                        "error_message": error_msg,
                    },
                )

                await self._emit(
                    EventType.TOOL_CALL_END,
                    {
                        "tool_call_id": tc_record_id,
                        # step_id 必须带出：前端据 TOOL_CALL_START 合成 tool_execution
                        # step，若 END 不带 step_id，该 step 永远停在 running。
                        "step_id": step_id,
                        "tool_name": tool_name,
                        "status": status,
                        "output": result_content if status == "success" else None,
                        "error": error_msg,
                        "duration_ms": duration_ms,
                    },
                    session_id,
                    run_id,
                )

                tool_results_all.append(
                    {
                        "tool_name": tool_name,
                        "arguments": args,
                        "output": result_content,
                        "status": status,
                        "duration_ms": duration_ms,
                        "error": error_msg,
                    }
                )

                # 失败时把错误详情回传给 LLM，使其能据此自愈
                # （_execute_single_tool 失败返回的 result_content 只有占位符，
                #   真正的 err_msg 仅在 error_message 里，不带上则 LLM 拿到空诊断）
                tool_content = result_content
                if status != "success" and error_msg:
                    tool_content = f"[工具 {tool_name} 失败]: {error_msg}"

                # 持久化 tool 消息
                await self._db.messages.save(
                    Message(
                        id=generate_time_id(),
                        session_id=session_id,
                        role=MessageRole.TOOL,
                        content=tool_content,
                        tool_call_id=tc_id,
                        run_id=run_id,
                        step_id=step_id,
                        tool_call_record_id=tc_record_id,
                        # 前端据此给工具气泡标名（避免跨表 join）
                        tool_name=tool_name,
                        timestamp=datetime.now(),
                    )
                )

                return ToolMessage(content=tool_content, tool_call_id=tc_id)

            except Exception as e:
                # 捕获未处理异常，保留原始 tc_id 以便 LLM 关联 tool_call → tool_message
                logger.error("tool_execution_failed", tool=tool_name, error=str(e))
                return ToolMessage(
                    content=f"[工具执行异常] {e}",
                    tool_call_id=tc_id,
                )

        # 并行执行
        tasks = [_execute_one(sn, sid, tc) for (sn, sid, tc) in tool_step_starts]
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
        tool_call_id: str,
    ) -> tuple[str, str, str | None, str | None]:
        """执行单个工具调用，失败时通过 Fallback 路由表尝试自愈.

        自愈流程:
        1. 白名单校验（子 Agent 防御性拒绝）
        2. 执行工具（带超时）
        3. 失败时查 Fallback 路由 → retry / alternate / passthrough / escalate
        4. 熔断器打开时直接返回失败

        Args:
            tool_name: 工具名称。
            args: 工具调用参数。
            session_id: 会话 ID。
            run_id: 运行 ID。
            tool_call_id: 工具调用记录 ID。

        Returns:
            四元组 ``(result_content, status, error_msg, error_stack)``:
            - result_content: 工具输出文本（成功）或错误描述（失败）。
            - status: "success" / "failed" / "denied"。
            - error_msg: 错误信息（成功时为 None）。
            - error_stack: 错误堆栈（成功时为 None）。
        """
        # 子 Agent 工具白名单校验：白名单外的工具一律拒绝执行（防御模型误调）
        if (
            self._allowed_tool_names is not None
            and tool_name not in self._allowed_tool_names
        ):
            return (
                f"[工具 {tool_name} 不在当前 Agent 的允许列表]",
                "failed",
                f"Tool '{tool_name}' is not allowed for this agent",
                None,
            )

        last_error: Exception | None = None
        params = dict(args)

        for attempt in range(2):  # 最多重试 1 次
            try:
                result = await asyncio.wait_for(
                    self._tool_manager.call_tool(
                        name=tool_name,
                        params=params,
                        session_id=session_id,
                        run_id=run_id,
                        tool_call_id=tool_call_id,
                    ),
                    timeout=self._harness_settings.tool_timeout,
                )
                output = result.output if result.output is not None else ""
                if result.status == "denied":
                    # 用户拒绝不是工具可靠性信号，不记入熔断器
                    return output, "denied", None, None
                if result.status != "success":
                    raise RuntimeError(
                        result.error
                        or f"工具 {tool_name} 返回失败状态: {result.status}"
                    )
                # 在确认 status 为 success 之后才记录，避免失败结果先被记成成功
                self._error_handler.record_result(tool_name, success=True)
                return output, "success", None, None
            except Exception as e:
                last_error = e
                self._error_handler.record_result(tool_name, success=False)

                # 熔断器三态: CLOSED → OPEN（失败率达阈值）→ HALF_OPEN（超时后探测）
                # OPEN 状态下直接跳过 Fallback，返回失败
                if self._error_handler.is_circuit_open(tool_name):
                    break

                # 查找 Fallback 路由（按 tool_name + error 关键词匹配）
                fallback = self._error_handler.get_fallback(tool_name, e)
                if fallback is None:
                    break

                action = fallback.action
                if action == "retry" and attempt < fallback.max_retries:
                    if fallback.param_transform:
                        params = self._error_handler.apply_param_transform(
                            params, fallback
                        )
                    logger.info(
                        "tool_retry_with_fallback", tool=tool_name, attempt=attempt + 1
                    )
                    continue
                elif action == "alternate" and fallback.alternate_tool:
                    try:
                        alt_result = await asyncio.wait_for(
                            self._tool_manager.call_tool(
                                name=fallback.alternate_tool,
                                params=params,
                                session_id=session_id,
                                run_id=run_id,
                                tool_call_id=tool_call_id,
                            ),
                            timeout=self._harness_settings.tool_timeout,
                        )
                        self._error_handler.record_result(
                            fallback.alternate_tool, success=True
                        )
                        output = alt_result.output or ""
                        return output, "success", None, None
                    except Exception as alt_err:
                        self._error_handler.record_result(
                            fallback.alternate_tool, success=False
                        )
                        last_error = alt_err
                        break
                elif action == "passthrough":
                    return f"[工具 {tool_name} 返回空结果]", "success", None, None
                else:  # escalate
                    break

        # 全部失败
        err_msg = str(last_error) if last_error else "未知错误"
        err_stack = traceback.format_exc() if last_error else None
        return f"[工具 {tool_name} 执行失败]", "failed", err_msg, err_stack

    def _assemble_response(
        self,
        stream_chunks: list[AIMessageChunk],
        fallback_content: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        """合并流式 chunk，提取完整 content 与 tool_calls.

        利用 langchain 的 AIMessageChunk.__add__ 拼接文本分片并累加 tool_call
        的 args JSON 分片，比手动拼装可靠。空流时回退到已累积的 fallback_content。

        Args:
            stream_chunks: LLM 流式返回的 AIMessageChunk 列表。
            fallback_content: 流为空时的回退文本（来自已累积的 full_content）。

        Returns:
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

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """请求停止当前运行（优雅退出）.

        设置内部停止事件，主循环在下一个检查点检测到后终止。
        与外部 stop_signal 等价，任一置位即终止。
        """
        self._stop_event.set()

    def _should_stop(self) -> bool:
        """检查是否应停止运行.

        Returns:
            True 表示内部 request_stop() 或外部 stop_signal 任一已置位。
        """
        return self._stop_event.is_set() or bool(
            self._stop_signal is not None and self._stop_signal.is_set()
        )

    async def _save_step(self, step: Step) -> None:
        """持久化步骤记录到数据库.

        自动注入 parent_run_id（子 Agent 运行时由 run() 设置）。
        持久化异常仅记录日志，不向上抛出。

        Args:
            step: 待持久化的 Step 实例。
        """
        try:
            # 步骤显式记录父 run（子 Agent 运行时由 run() 注入）
            if step.parent_run_id is None:
                step.parent_run_id = self._parent_run_id
            await self._db.steps.save(step)
        except Exception as e:
            logger.error("save_step_failed", error=str(e))

    async def _update_step(self, step_id: str, updates: dict[str, Any]) -> None:
        """更新已有步骤记录的部分字段.

        Args:
            step_id: 步骤 ID。
            updates: 待更新的字段字典，键为字段名，值为新值。
        """
        try:
            await self._db.steps.update(step_id, updates)
        except Exception as e:
            logger.error("update_step_failed", step_id=step_id, error=str(e))

    async def _emit(
        self,
        event_type: EventType,
        data: dict[str, Any],
        session_id: str,
        run_id: str,
    ) -> None:
        """向前端推送事件（WebSocket）.

        推送异常仅记录警告日志，不向上抛出。

        Args:
            event_type: 事件类型枚举。
            data: 事件数据字典。
            session_id: 会话 ID。
            run_id: 运行 ID。
        """
        try:
            await self._ws.send_to_session(
                session_id,
                build_event(event_type, data, session_id=session_id, run_id=run_id),
            )
        except Exception as e:
            logger.warning(
                "emit_event_failed", event_type=str(event_type), error=str(e)
            )

    async def _emit_llm_call_end(
        self,
        step_id: str,
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

        Args:
            step_id: LLM 调用步骤 ID。
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
                "step_id": step_id,
                "status": status,
                "duration_ms": duration_ms,
                "tool_calls_count": tool_calls_count,
                "tool_calls": tool_calls or [],
            },
            session_id,
            run_id,
        )
