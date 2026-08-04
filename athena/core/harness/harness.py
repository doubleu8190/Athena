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
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from athena.config.settings import Settings, get_settings
from athena.core.compression.compressor import ContextCompressor
from athena.core.harness.budget import Budget, BudgetExceeded
from athena.core.harness.error_handler import ToolErrorHandler
from athena.core.llm.provider import LLMProvider
from athena.core.tools.manager import UnifiedToolManager
from athena.db.database import Database
from athena.gateway.ws.manager import WebSocketManager
from athena.models.step import StepStatus, StepType
from athena.models.tool import ToolCallStatus
from athena.schemas.events import EventType, build_event
from athena.utils.ids import RunIdGenerator
from athena.utils.logging import get_logger

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
        messages: list[dict[str, Any]],
        session_id: str,
        system_prompt: str = "",
        run_id: str | None = None,
    ) -> HarnessRunResult:
        """执行单次 Agent 运行.

        Args:
            messages: 对话历史（含最新用户消息）
            session_id: 会话 ID
            system_prompt: 系统提示词
            run_id: 可选 run_id（子 Agent 使用），未提供则生成主 run_id
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
            lc_messages.append(self._dict_to_message(m))

        # 绑定工具
        lc_tools = self._tool_manager.get_langchain_tools()
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
                    dict_messages = [self._message_to_dict(m) for m in lc_messages]
                    compressed = await self._compressor.compress(
                        dict_messages,
                        session_id=session_id,
                    )
                    if len(compressed) < len(dict_messages):
                        lc_messages = [self._dict_to_message(m) for m in compressed]

                # Step: LLM 调用
                step_counter += 1
                llm_step_id = str(uuid.uuid4())
                budget.increment_turn()

                await self._save_step({
                    "id": llm_step_id,
                    "session_id": session_id,
                    "run_id": rid,
                    "step_number": step_counter,
                    "step_type": str(StepType.LLM_CALL),
                    "status": str(StepStatus.RUNNING),
                    "started_at": datetime.now().isoformat(),
                })

                await self._emit(
                    EventType.LLM_CALL_START,
                    {"step_id": llm_step_id, "turn": budget.turn_count},
                    session_id,
                    rid,
                )

                start_time = time.time()
                full_content = ""
                tool_calls_raw: list[dict[str, Any]] = []
                try:
                    async for chunk in bound_llm.astream(lc_messages):
                        if self._stop_event.is_set():
                            break
                        chunk_content = getattr(chunk, "content", "")
                        if isinstance(chunk_content, list):
                            chunk_content = "".join(
                                c.get("text", "") if isinstance(c, dict) else str(c)
                                for c in chunk_content
                            )
                        if chunk_content:
                            full_content += chunk_content
                            await self._emit(
                                EventType.LLM_TOKEN,
                                {"token": chunk_content},
                                session_id,
                                rid,
                            )
                        # 收集 tool_calls（最后一块通常含完整 tool_calls）
                        chunk_tc = getattr(chunk, "tool_call_chunks", None) or []
                        for tc_chunk in chunk_tc:
                            if isinstance(tc_chunk, dict):
                                tool_calls_raw.append(tc_chunk)

                    # 从最终 AIMessage 提取 tool_calls
                    ai_message = AIMessage(content=full_content)
                    final_tc = self._extract_tool_calls(bound_llm, lc_messages)
                    if final_tc:
                        ai_message = AIMessage(content=full_content, tool_calls=final_tc)
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

                await self._emit(
                    EventType.LLM_CALL_END,
                    {
                        "step_id": llm_step_id,
                        "duration_ms": duration_ms,
                        "tool_calls_count": len(final_tc),
                    },
                    session_id,
                    rid,
                )

                # 持久化 assistant 消息（中断恢复关键）
                if self._db is not None:
                    await self._db.save_message(session_id, {
                        "id": str(uuid.uuid4()),
                        "role": "assistant",
                        "content": full_content,
                        "tool_calls": final_tc,
                        "metadata": {"step_id": llm_step_id, "run_id": rid},
                        "timestamp": datetime.now().isoformat(),
                    })

                # 没有工具调用 → 终止循环
                if not final_tc:
                    break

                # Step: 工具执行（并行）
                # 预分配 step_number（project_memory 约束：避免并行 race condition）
                tool_step_starts: list[tuple[int, str, dict[str, Any]]] = []
                for tc in final_tc:
                    step_counter += 1
                    tool_step_id = str(uuid.uuid4())
                    tool_step_starts.append((step_counter, tool_step_id, tc))

                tool_messages = await self._execute_tool_calls(
                    final_tc=final_tc,
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
        final_tc: list[dict[str, Any]],
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
            tc_id = tc.get("id", str(uuid.uuid4()))

            # 创建 tool_execution step
            await self._save_step({
                "id": step_id,
                "session_id": session_id,
                "run_id": run_id,
                "step_number": step_number,
                "step_type": str(StepType.TOOL_EXECUTION),
                "parent_step_id": parent_step_id,
                "status": str(StepStatus.RUNNING),
                "started_at": datetime.now().isoformat(),
            })

            # 创建 tool_call 记录
            tc_record_id = str(uuid.uuid4())
            if self._db is not None:
                await self._db.save_tool_call({
                    "id": tc_record_id,
                    "session_id": session_id,
                    "step_id": step_id,
                    "tool_name": tool_name,
                    "arguments": args,
                    "status": str(ToolCallStatus.RUNNING),
                    "started_at": datetime.now().isoformat(),
                })

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
                await self._db.save_message(session_id, {
                    "id": str(uuid.uuid4()),
                    "role": "tool",
                    "content": result_content,
                    "tool_call_id": tc_id,
                    "metadata": {"step_id": step_id, "tool_call_record_id": tc_record_id},
                    "timestamp": datetime.now().isoformat(),
                })

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
                    tool_call_id=str(uuid.uuid4()),
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

    def _extract_tool_calls(
        self, bound_llm: LLMProvider, messages: list[BaseMessage]
    ) -> list[dict[str, Any]]:
        """从最后一次 LLM 调用提取完整 tool_calls.

        简化实现：流式 chunk 中累积的 tool_call_chunks 在最后一条 AIMessage 中提取。
        本实现采用重新绑定并使用非流式 ainvoke 的兜底方式获取完整 tool_calls。
        """
        # 优先从最后一条 AIMessage 读取（如果 astreem chunk 已聚合）
        if messages and isinstance(messages[-1], AIMessage):
            tcs = getattr(messages[-1], "tool_calls", None) or []
            if tcs:
                return [
                    {
                        "id": tc.get("id", str(uuid.uuid4())),
                        "name": tc.get("name", ""),
                        "args": tc.get("args", {}) or tc.get("arguments", {}) or {},
                    }
                    for tc in tcs
                ]
        return []

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        """请求停止当前运行（优雅退出）."""
        self._stop_event.set()

    async def _save_step(self, step: dict[str, Any]) -> None:
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

    def _dict_to_message(self, m: dict[str, Any]) -> BaseMessage:
        """字典 → LangChain Message."""
        role = m.get("role", "user")
        content = m.get("content", "")
        tool_calls = m.get("tool_calls") or []
        tool_call_id = m.get("tool_call_id")
        if role == "user":
            return HumanMessage(content=content)
        if role == "system":
            return SystemMessage(content=content)
        if role == "assistant":
            if tool_calls:
                lc_tcs = [
                    {
                        "id": tc.get("id", str(uuid.uuid4())),
                        "name": tc.get("name", ""),
                        "args": tc.get("args", {}) or tc.get("arguments", {}) or {},
                        "type": "tool_call",
                    }
                    for tc in tool_calls
                ]
                return AIMessage(content=content, tool_calls=lc_tcs)
            return AIMessage(content=content)
        if role == "tool":
            return ToolMessage(content=content, tool_call_id=tool_call_id or "")
        return HumanMessage(content=content)

    def _message_to_dict(self, m: BaseMessage) -> dict[str, Any]:
        """LangChain Message → 字典."""
        role_map = {
            HumanMessage: "user",
            SystemMessage: "system",
            AIMessage: "assistant",
            ToolMessage: "tool",
        }
        role = "user"
        for cls, r in role_map.items():
            if isinstance(m, cls):
                role = r
                break
        d: dict[str, Any] = {"role": role, "content": getattr(m, "content", "")}
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            d["tool_calls"] = [
                {"id": tc.get("id", ""), "name": tc.get("name", ""), "args": tc.get("args", {})}
                for tc in m.tool_calls
            ]
        if isinstance(m, ToolMessage):
            d["tool_call_id"] = getattr(m, "tool_call_id", "")
        return d

    def _estimate_tokens(self, messages: list[BaseMessage]) -> int:
        """粗略估算 token 数（project_memory: len/4 回退方案）."""
        total = 0
        for m in messages:
            if isinstance(m, BaseMessage):
                total += len(getattr(m, "content", "") or "") // 4
            elif isinstance(m, dict):
                total += len(str(m.get("content", ""))) // 4
        return total
