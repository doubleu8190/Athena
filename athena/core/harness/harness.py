"""Harness 执行引擎 — 自定义 while 循环 + 预算控制 + step 日志 + 事件推送.

核心职责：
- 执行 LLM 调用（流式）
- 编排工具调用（并行执行无依赖工具）
- 预算控制（maxTurns + retryBudget）
- 事件流式推送到前端
- 执行过程日志持久化到 SQLite

project_memory 约束：
- step_number 在 run_id 范围内全局递增，并行工具调用前预分配序号
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

from athena.config.settings import Settings, get_settings
from athena.core.compression.compressor import ContextCompressor
from athena.core.harness.budget import Budget, BudgetExceeded
from athena.core.harness.error_handler import ToolErrorHandler
from athena.core.llm.provider import LLMProvider
from athena.core.tools.manager import UnifiedToolManager
from athena.db.database import Database
from athena.gateway.ws.manager import WebSocketManager
from athena.models import Message, MessageRole, Step, ToolCallRecord
from athena.models.step import StepStatus, StepType
from athena.models.tool import ToolCallStatus
from athena.schemas.events import EventType, build_event
from athena.utils.ids import RunIdGenerator, generate_time_id
from athena.utils.llm import extract_message_text
from athena.utils.logging import get_logger
from athena.utils.message import dict_to_message

logger = get_logger(__name__)


@dataclass
class HarnessSettings:
    """Harness 配置."""

    max_turns_per_run: int = 20
    retry_budget: int = 3
    tool_timeout: int = 60


@dataclass
class HarnessRunResult:
    """单次 Harness 运行结果."""

    content: str
    run_id: str
    turn_count: int
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    interrupted: bool = False


class Harness:
    """Agent 执行引擎."""

    def __init__(
        self,
        llm: LLMProvider,
        tool_manager: UnifiedToolManager,
        settings: Settings | None = None,
        db: Database | None = None,
        ws_manager: WebSocketManager | None = None,
        compressor: ContextCompressor | None = None,
        error_handler: ToolErrorHandler | None = None,
        harness_settings: HarnessSettings | None = None,
    ) -> None:
        self._llm = llm
        self._tool_manager = tool_manager
        self._settings = settings or get_settings()
        self._db = db
        self._ws = ws_manager
        self._compressor = compressor
        self._error_handler = error_handler or ToolErrorHandler()
        self._harness_settings = harness_settings or HarnessSettings(
            max_turns_per_run=self._settings.max_turns_per_run,
            retry_budget=self._settings.retry_budget,
            tool_timeout=self._settings.tool_timeout,
        )
        self._stop_event = asyncio.Event()
        self._allowed_tool_names: set[str] | None = None
        self._register_default_routes()

    def _register_default_routes(self) -> None:
        """注册默认 Fallback 路由."""
        self._error_handler.register_fallback(
            tool_name="exec_shell",
            on_errors=["timeout", "timed out"],
            action="retry",
            max_retries=1,
            param_transform={"timeout": lambda t: (t * 2) if isinstance(t, (int, float)) else t},
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
        tool_names: list[str] | None = None,
    ) -> HarnessRunResult:
        """执行单次 Agent 运行.

        Args:
            messages: 对话历史（含最新用户消息）
            session_id: 会话 ID
            system_prompt: 系统提示词
            run_id: 可选 run_id（子 Agent 使用），未提供则生成主 run_id
            tool_names: 可选工具白名单（子 Agent 使用），None 表示全部工具
        """
        rid = run_id or RunIdGenerator.generate_main_run_id()
        budget = Budget(
            max_turns=self._harness_settings.max_turns_per_run,
            retry_budget=self._harness_settings.retry_budget,
        )
        self._stop_event.clear()

        # 更新会话状态为 running
        if self._db is not None:
            await self._db.update_session(session_id, status="running", run_id=rid)

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
            while not self._stop_event.is_set():
                # 上下文压缩
                if self._compressor is not None:
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

                await self._save_step(Step(
                    id=llm_step_id,
                    session_id=session_id,
                    run_id=rid,
                    step_number=step_counter,
                    step_type=StepType.LLM_CALL,
                    status=StepStatus.RUNNING,
                    started_at=datetime.now(),
                ))

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
                    async for chunk in bound_llm.astream(lc_messages):
                        if self._stop_event.is_set():
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
                    if merged_content:
                        full_content = merged_content

                    # 空响应检测：无文本且无工具调用 → 不落库、有界重试
                    if not full_content.strip() and not final_tc:
                        error_msg = "LLM 返回空响应（无内容且无工具调用）"
                        logger.warning("llm_empty_response", run_id=rid, error=error_msg)
                        await self._update_step(llm_step_id, {
                            "status": str(StepStatus.FAILED),
                            "completed_at": datetime.now().isoformat(),
                            "duration_ms": (time.time() - start_time) * 1000,
                            "error_message": error_msg,
                        })
                        await self._emit(
                            EventType.ERROR,
                            {"step_id": llm_step_id, "error": error_msg, "phase": "llm_call"},
                            session_id,
                            rid,
                        )
                        # 失败路径同样需结束 LLM 调用生命周期：前端据此移除
                        # 本次调用创建的流式气泡，避免重试残留空气泡
                        await self._emit_llm_call_end(
                            llm_step_id, status="failed", session_id=session_id, run_id=rid
                        )
                        if budget.remaining_retries() > 0:
                            budget.increment_retry()
                            continue
                        break

                    ai_message = AIMessage(
                        content=full_content, tool_calls=final_tc
                    ) if final_tc else AIMessage(content=full_content)
                    lc_messages.append(ai_message)
                    last_content = full_content
                    error_msg = None

                except Exception as e:
                    logger.error("llm_call_failed", error=str(e), run_id=rid)
                    error_msg = f"LLM 调用失败: {e}"
                    await self._update_step(llm_step_id, {
                        "status": str(StepStatus.FAILED),
                        "completed_at": datetime.now().isoformat(),
                        "duration_ms": (time.time() - start_time) * 1000,
                        "error_message": str(e),
                    })
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
                await self._update_step(llm_step_id, {
                    "status": str(StepStatus.COMPLETED),
                    "completed_at": datetime.now().isoformat(),
                    "duration_ms": duration_ms,
                    "llm_input_tokens": self._estimate_tokens(lc_messages[:-1]),
                    "llm_output_tokens": self._estimate_tokens([ai_message]),
                })

                await self._emit_llm_call_end(
                    llm_step_id,
                    status="completed",
                    session_id=session_id,
                    run_id=rid,
                    duration_ms=duration_ms,
                    tool_calls_count=len(final_tc),
                )

                # 持久化 assistant 消息（中断恢复关键）
                # 守卫：仅在确实有输出（文本或工具调用）时落库，避免空消息污染对话
                if self._db is not None and (full_content.strip() or final_tc):
                    await self._db.save_message(Message(
                        id=generate_time_id(),
                        session_id=session_id,
                        role=MessageRole.ASSISTANT,
                        content=full_content,
                        tool_calls=final_tc,
                        metadata={"step_id": llm_step_id, "run_id": rid},
                        timestamp=datetime.now(),
                    ))

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

                if self._stop_event.is_set():
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

        # 后处理
        await self._emit(
            EventType.STREAM_END,
            {"run_id": rid, "turn_count": budget.turn_count, "error": error_msg},
            session_id,
            rid,
        )

        if self._db is not None:
            await self._db.update_session(session_id, status="idle" if not interrupted else "interrupted")

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
        """并行执行所有工具调用，同时记录日志与推送事件."""

        async def _execute_one(
            step_number: int, step_id: str, tc: dict[str, Any]
        ) -> ToolMessage:
            tool_name = tc.get("name", "")
            args = tc.get("args", {}) or tc.get("arguments", {}) or {}
            tc_id = tc.get("id", generate_time_id())

            # 创建 tool_execution step
            await self._save_step(Step(
                id=step_id,
                session_id=session_id,
                run_id=run_id,
                step_number=step_number,
                step_type=StepType.TOOL_EXECUTION,
                parent_step_id=parent_step_id,
                status=StepStatus.RUNNING,
                started_at=datetime.now(),
            ))

            # 创建 tool_call 记录
            tc_record_id = generate_time_id()
            if self._db is not None:
                await self._db.save_tool_call(ToolCallRecord(
                    id=tc_record_id,
                    session_id=session_id,
                    step_id=step_id,
                    tool_name=tool_name,
                    arguments=args,
                    status=ToolCallStatus.RUNNING,
                    started_at=datetime.now(),
                ))

            await self._emit(
                EventType.TOOL_CALL_START,
                {
                    "tool_call_id": tc_record_id,
                    "step_id": step_id,
                    "tool_name": tool_name,
                    "arguments": args,
                },
                session_id,
                run_id,
            )

            start_time = time.time()
            result_content, status, error_msg, error_stack = await self._execute_single_tool(
                tool_name=tool_name,
                args=args,
                session_id=session_id,
                run_id=run_id,
                tool_call_id=tc_record_id,
            )
            duration_ms = (time.time() - start_time) * 1000

            # 更新 tool_call 记录
            if self._db is not None:
                await self._db.update_tool_call(tc_record_id, {
                    "raw_output": result_content if status == "success" else None,
                    "status": status,
                    "completed_at": datetime.now().isoformat(),
                    "duration_ms": duration_ms,
                    "error_message": error_msg,
                    "error_stack": error_stack,
                })

            # 更新 tool_execution step
            await self._update_step(step_id, {
                "status": str(StepStatus.COMPLETED) if status == "success" else str(StepStatus.FAILED),
                "completed_at": datetime.now().isoformat(),
                "duration_ms": duration_ms,
                "error_message": error_msg,
            })

            await self._emit(
                EventType.TOOL_CALL_END,
                {
                    "tool_call_id": tc_record_id,
                    "tool_name": tool_name,
                    "status": status,
                    "output": result_content if status == "success" else None,
                    "error": error_msg,
                    "duration_ms": duration_ms,
                },
                session_id,
                run_id,
            )

            tool_results_all.append({
                "tool_name": tool_name,
                "arguments": args,
                "output": result_content,
                "status": status,
                "duration_ms": duration_ms,
                "error": error_msg,
            })

            # 持久化 tool 消息
            if self._db is not None:
                await self._db.save_message(Message(
                    id=generate_time_id(),
                    session_id=session_id,
                    role=MessageRole.TOOL,
                    content=result_content,
                    tool_call_id=tc_id,
                    metadata={"step_id": step_id, "tool_call_record_id": tc_record_id},
                    timestamp=datetime.now(),
                ))

            return ToolMessage(content=result_content, tool_call_id=tc_id)

        # 并行执行
        tasks = [
            _execute_one(sn, sid, tc)
            for (sn, sid, tc) in tool_step_starts
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        tool_messages: list[ToolMessage] = []
        for r in results:
            if isinstance(r, BaseException):
                logger.error("tool_execution_failed", error=str(r))
                tool_messages.append(ToolMessage(
                    content=f"[工具执行异常] {r}",
                    tool_call_id=generate_time_id(),
                ))
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
        """执行单个工具，失败时尝试自愈.

        project_memory 约束：自愈路由器集成到本方法，工具失败时先查 Fallback 路由表。
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
                self._error_handler.record_result(tool_name, success=True)
                output = result.output if result.output is not None else ""
                if result.status == "denied":
                    return output, "denied", None, None
                if result.status != "success":
                    raise RuntimeError(result.error or f"工具 {tool_name} 返回失败状态: {result.status}")
                return output, "success", None, None
            except Exception as e:
                last_error = e
                self._error_handler.record_result(tool_name, success=False)

                # 熔断器打开 → 直接抛出
                if self._error_handler.is_circuit_open(tool_name):
                    break

                # 查找 Fallback 路由
                fallback = self._error_handler.get_fallback(tool_name, e)
                if fallback is None:
                    break

                action = fallback.action
                if action == "retry" and attempt < fallback.max_retries:
                    if fallback.param_transform:
                        params = self._error_handler.apply_param_transform(params, fallback)
                    logger.info("tool_retry_with_fallback", tool=tool_name, attempt=attempt + 1)
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
                        self._error_handler.record_result(fallback.alternate_tool, success=True)
                        output = alt_result.output or ""
                        return output, "success", None, None
                    except Exception as alt_err:
                        self._error_handler.record_result(fallback.alternate_tool, success=False)
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

        langchain 的 AIMessageChunk.__add__ 会拼接文本分片并累加 tool_call
        的 args JSON 分片，比手动拼装可靠。空流时回退到已累积的 full_content。
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
        """将 langchain tool_calls 归一化为内部格式."""
        return [
            {
                "id": tc.get("id", generate_time_id()),
                "name": tc.get("name", ""),
                "args": tc.get("args", {}) or tc.get("arguments", {}) or {},
            }
            for tc in tool_calls
        ]

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """请求停止当前运行（优雅退出）."""
        self._stop_event.set()

    async def _save_step(self, step: Step) -> None:
        if self._db is None:
            return
        try:
            await self._db.save_step(step)
        except Exception as e:
            logger.error("save_step_failed", error=str(e))

    async def _update_step(self, step_id: str, updates: dict[str, Any]) -> None:
        if self._db is None:
            return
        try:
            await self._db.update_step(step_id, updates)
        except Exception as e:
            logger.error("update_step_failed", step_id=step_id, error=str(e))

    async def _emit(
        self,
        event_type: EventType,
        data: dict[str, Any],
        session_id: str,
        run_id: str,
    ) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send_to_session(
                session_id,
                build_event(event_type, data, session_id=session_id, run_id=run_id),
            )
        except Exception as e:
            logger.warning("emit_event_failed", event_type=str(event_type), error=str(e))

    async def _emit_llm_call_end(
        self,
        step_id: str,
        status: str,
        session_id: str,
        run_id: str,
        duration_ms: float = 0,
        tool_calls_count: int = 0,
    ) -> None:
        """推送 LLM 调用结束事件（status: completed / failed）.

        前端据此结束流式气泡：status=failed 时移除本次调用创建的无内容
        building 气泡，避免空响应/异常重试路径在界面上残留空气泡。
        成功路径与失败路径都必须调用，保证事件生命周期成对。
        """
        await self._emit(
            EventType.LLM_CALL_END,
            {
                "step_id": step_id,
                "status": status,
                "duration_ms": duration_ms,
                "tool_calls_count": tool_calls_count,
            },
            session_id,
            run_id,
        )

    def _estimate_tokens(self, messages: list[BaseMessage]) -> int:
        """粗略估算 token 数（project_memory: len/4 回退方案）."""
        total = 0
        for m in messages:
            if isinstance(m, BaseMessage):
                total += len(getattr(m, "content", "") or "") // 4
            elif isinstance(m, dict):
                total += len(str(m.get("content", ""))) // 4
        return total
