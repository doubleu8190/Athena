"""可被 LangGraph 单轮节点复用的 Harness 执行器。

``Harness`` 保留旧的整轮兼容入口；本模块只抽取一次 LLM 调用、一次工具
批次和一次执行收尾。所有跨节点状态都由调用方显式传入，避免把 Python
运行时对象写入 LangGraph checkpoint。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
)
from langchain_core.tools import StructuredTool

from athena.contracts.events import EventType
from athena.core.harness.harness import Harness
from athena.models import Message, MessageRole
from athena.utils.llm import extract_message_text
from athena.utils.message import dict_to_message, message_to_dict
from athena.runtime.streaming import StreamCoalescer
from athena.runtime.orchestration import (
    DELEGATION_TOOL_NAMES,
    PLAN_SUBMISSION_TOOL_NAME,
    PlanSubmission,
)


def _to_domain_messages(
    messages: Sequence[Message | dict[str, Any]],
    *,
    session_id: str,
    run_id: str,
) -> list[Message]:
    """将 checkpoint 中的消息字典补全为领域消息。"""

    result: list[Message] = []
    for message in messages:
        if isinstance(message, Message):
            result.append(message)
            continue
        payload = dict(message)
        payload.setdefault("id", f"{run_id}:message:{len(result)}")
        payload.setdefault("session_id", session_id)
        payload.setdefault("run_id", run_id)
        payload.setdefault("timestamp", datetime.now())
        result.append(Message.model_validate(payload))
    return result


@dataclass
class LlmTurnOutcome:
    """一次 LLM 调用的 JSON 边界结果。"""

    messages: list[dict[str, Any]]
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    interrupted: bool = False
    retryable: bool = False
    turn_count: int = 0
    retry_count: int = 0
    stream_started: bool = False
    stream_version: int = 0
    stream_offset: int = 0
    route: str = "agent_loop"
    plan_request: dict[str, Any] | None = None


@dataclass
class ToolBatchOutcome:
    """一批并行工具调用的 JSON 边界结果。"""

    messages: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    interrupted: bool = False


class HarnessTurnExecutor(Harness):
    """将旧 Harness 的一次调用拆成可检查点化的执行步骤。"""

    def _configure_context(
        self,
        *,
        session_id: str,
        run_id: str,
        message_id: str | None,
        stop_signal: asyncio.Event,
        parent_run_id: str | None,
        tool_names: list[str] | None,
        stream_version: int,
        stream_offset: int,
    ) -> None:
        self._parent_run_id = parent_run_id
        self._stable_tool_ids = True
        self._message_id = message_id
        self._stop_signal = stop_signal
        self._allowed_tool_names = set(tool_names) if tool_names else None
        self._answer_stream_id = f"answer-{run_id}"
        self._answer_stream = StreamCoalescer(
            session_id=session_id,
            run_id=run_id,
            stream_id=self._answer_stream_id,
            stream_type="answer",
            message_id=message_id,
            publish=self._events.publish,
            initial_version=stream_version,
            initial_offset=stream_offset,
        )

    def _configure_tool_context(
        self,
        *,
        message_id: str | None,
        stop_signal: asyncio.Event,
        parent_run_id: str | None,
        tool_names: list[str] | None,
    ) -> None:
        """配置工具批次需要的运行上下文，不创建 answer stream。

        工具节点只发布工具和 thinking 事件，不会向回答流追加文本；因此
        不应接收或重建回答流的版本和偏移量。
        """

        self._parent_run_id = parent_run_id
        self._stable_tool_ids = True
        self._message_id = message_id
        self._stop_signal = stop_signal
        self._allowed_tool_names = set(tool_names) if tool_names else None

    async def _start_stream_if_needed(
        self,
        *,
        session_id: str,
        run_id: str,
        stream_started: bool,
    ) -> bool:
        if stream_started:
            return True
        await self._emit(
            EventType.STREAM_START,
            {
                "run_id": run_id,
                "stream_id": self._answer_stream_id,
                "stream_type": "answer",
            },
            session_id,
            run_id,
        )
        await self._emit_thinking(
            EventType.THINKING_STARTED,
            "正在准备请求",
            session_id,
            run_id,
        )
        return True

    def _llm_outcome(
        self,
        *,
        messages: list[dict[str, Any]],
        stream_started: bool,
        turn_count: int,
        retry_count: int,
        content: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        error: str | None = None,
        interrupted: bool = False,
        retryable: bool = False,
    ) -> LlmTurnOutcome:
        """构造单轮结果并集中复制回答流的检查点状态。"""

        if self._answer_stream is None:
            raise RuntimeError(
                "answer stream must be configured before creating an outcome"
            )
        return LlmTurnOutcome(
            messages=messages,
            content=content,
            tool_calls=tool_calls or [],
            error=error,
            interrupted=interrupted,
            retryable=retryable,
            turn_count=turn_count,
            retry_count=retry_count,
            stream_started=stream_started,
            stream_version=self._answer_stream.version,
            stream_offset=self._answer_stream.offset,
        )

    @staticmethod
    def _stable_tool_call_id(
        run_id: str, turn_count: int, index: int, tool_call: dict[str, Any]
    ) -> str:
        raw = f"{run_id}:{turn_count}:{index}:{tool_call.get('name', '')}:{tool_call.get('args', {})}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        return f"{run_id}:tool:{turn_count}:{index}:{digest}"

    def _assemble_stable_response(
        self,
        chunks: list[AIMessageChunk],
        *,
        run_id: str,
        turn_count: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        if not chunks:
            return "", []
        merged = chunks[0]
        for chunk in chunks[1:]:
            merged = merged + chunk
        content = extract_message_text(merged)
        raw_tool_calls = merged.tool_calls or []
        tool_calls: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_tool_calls):
            args = raw.get("args", {}) or {}
            tool_call = {
                "id": raw.get("id") or "",
                "name": raw.get("name", ""),
                "args": args,
            }
            if not tool_call["id"]:
                tool_call["id"] = self._stable_tool_call_id(
                    run_id, turn_count, index, tool_call
                )
            tool_calls.append(tool_call)
        return content, tool_calls

    @staticmethod
    async def _submit_plan_placeholder(**_: Any) -> str:
        """仅用于向顶层 LLM 暴露控制协议，永远不会被执行。"""
        raise RuntimeError("submit_plan is a runtime control tool")

    def _get_llm_tools(
        self,
        *,
        tool_names: list[str] | None,
        parent_run_id: str | None,
    ) -> list[StructuredTool]:
        tools = [
            tool
            for tool in self._tool_manager.get_langchain_tools(names=tool_names)
            if tool.name not in DELEGATION_TOOL_NAMES
        ]
        if parent_run_id is None:
            tools.append(
                StructuredTool.from_function(
                    coroutine=self._submit_plan_placeholder,
                    name=PLAN_SUBMISSION_TOOL_NAME,
                    description=(
                        "Submit a validated execution plan for independent subtasks. "
                        "Use only when the request genuinely requires parallel work."
                    ),
                    args_schema=PlanSubmission,
                )
            )
        return tools

    async def run_llm_turn(
        self,
        *,
        messages: Sequence[Message | dict[str, Any]],
        session_id: str,
        run_id: str,
        system_prompt: str,
        turn_count: int,
        retry_count: int,
        max_turns: int,
        max_retries: int,
        stop_signal: asyncio.Event,
        stream_started: bool,
        stream_version: int,
        stream_offset: int,
        parent_run_id: str | None = None,
        tool_names: list[str] | None = None,
    ) -> LlmTurnOutcome:
        """执行一次 LLM 调用，返回下一图节点所需的 JSON 状态。"""

        self._configure_context(
            session_id=session_id,
            run_id=run_id,
            message_id=self._message_id_from_messages(messages),
            stop_signal=stop_signal,
            parent_run_id=parent_run_id,
            tool_names=tool_names,
            stream_version=stream_version,
            stream_offset=stream_offset,
        )
        stream_started = await self._start_stream_if_needed(
            session_id=session_id,
            run_id=run_id,
            stream_started=stream_started,
        )

        domain_messages = _to_domain_messages(
            messages,
            session_id=session_id,
            run_id=run_id,
        )
        compressed_domain = await self._compressor.compress(
            domain_messages,
            session_id=session_id,
        )
        compressed: list[BaseMessage] = []
        if system_prompt:
            compressed.append(SystemMessage(content=system_prompt))
        compressed.extend(dict_to_message(message) for message in compressed_domain)

        next_turn = turn_count + 1
        if next_turn > max_turns:
            error = f"轮次预算超限: {next_turn}/{max_turns}"
            await self._emit(
                EventType.BUDGET_EXCEEDED,
                {"reason": error, "turn_count": turn_count},
                session_id,
                run_id,
            )
            return self._llm_outcome(
                messages=self._conversation_dicts(
                    compressed, system_prompt=system_prompt
                ),
                error=error,
                stream_started=stream_started,
                turn_count=turn_count,
                retry_count=retry_count,
            )

        llm_call_id = f"{run_id}:llm:{next_turn}"
        await self._emit(
            EventType.LLM_CALL_START,
            {"call_id": llm_call_id, "turn": next_turn},
            session_id,
            run_id,
        )
        await self._emit_thinking(
            EventType.THINKING_SUMMARY,
            "正在生成回答",
            session_id,
            run_id,
        )

        full_content = ""
        chunks: list[AIMessageChunk] = []
        started_at = time.time()
        bound_tools = self._get_llm_tools(
            tool_names=tool_names,
            parent_run_id=parent_run_id,
        )
        bound_llm = self._llm.bind_tools(bound_tools) if bound_tools else self._llm
        try:
            async with asyncio.timeout(self._harness_settings.llm_stream_timeout):
                async for chunk in bound_llm.astream(compressed):
                    if self._should_stop():
                        break
                    if isinstance(chunk, AIMessageChunk):
                        chunks.append(chunk)
                    chunk_content = extract_message_text(chunk)
                    if chunk_content and not (
                        next_turn == 1
                        and parent_run_id is None
                    ):
                        full_content += chunk_content
                        if self._answer_stream is not None:
                            await self._answer_stream.append(chunk_content)
                    elif chunk_content:
                        full_content += chunk_content

            content, tool_calls = self._assemble_stable_response(
                chunks,
                run_id=run_id,
                turn_count=next_turn,
            )
            if content:
                full_content = content

            if self._should_stop():
                await self._emit_llm_call_end(
                    llm_call_id,
                    status="failed",
                    session_id=session_id,
                    run_id=run_id,
                )
                if self._answer_stream is not None:
                    await self._answer_stream.flush()

                return self._llm_outcome(
                    messages=self._conversation_dicts(
                        compressed, system_prompt=system_prompt
                    ),
                    interrupted=True,
                    stream_started=stream_started,
                    turn_count=turn_count,
                    retry_count=retry_count,
                )

            if not full_content.strip() and not tool_calls:
                error = "LLM 返回空响应（无内容且无工具调用）"
                await self._emit(
                    EventType.RUN_FAILED,
                    {"call_id": llm_call_id, "error": error, "phase": "llm_call"},
                    session_id,
                    run_id,
                )
                await self._emit_llm_call_end(
                    llm_call_id,
                    status="failed",
                    session_id=session_id,
                    run_id=run_id,
                )
                if self._answer_stream is not None:
                    await self._answer_stream.flush()
                next_retry = retry_count + 1
                return self._llm_outcome(
                    messages=self._conversation_dicts(
                        compressed, system_prompt=system_prompt
                    ),
                    error=error,
                    retryable=next_retry <= max_retries,
                    stream_started=stream_started,
                    turn_count=next_turn,
                    retry_count=next_retry,
                )

            ai_message = AIMessage(content=full_content, tool_calls=tool_calls)
            message_dicts = self._conversation_dicts(
                compressed,
                system_prompt=system_prompt,
            )
            message_dicts.append(message_to_dict(ai_message))
            await self._persist_assistant_message(
                session_id=session_id,
                run_id=run_id,
                turn_count=next_turn,
                content=full_content,
                tool_calls=tool_calls,
            )
            await self._emit_llm_call_end(
                llm_call_id,
                status="completed",
                session_id=session_id,
                run_id=run_id,
                duration_ms=(time.time() - started_at) * 1000,
                tool_calls_count=len(tool_calls),
                tool_calls=tool_calls,
            )
            if self._answer_stream is not None:
                await self._answer_stream.flush()
            plan_calls = [
                call
                for call in tool_calls
                if call.get("name") == PLAN_SUBMISSION_TOOL_NAME
            ]
            if plan_calls:
                if parent_run_id is not None:
                    return self._llm_outcome(
                        messages=message_dicts,
                        content=full_content,
                        error="submit_plan 只能由顶层 Agent 调用",
                        retryable=False,
                        stream_started=stream_started,
                        turn_count=next_turn,
                        retry_count=retry_count,
                    )
                if len(plan_calls) != 1 or len(plan_calls) != len(tool_calls):
                    return self._llm_outcome(
                        messages=message_dicts,
                        content=full_content,
                        error="submit_plan 不能与普通工具调用混用",
                        retryable=False,
                        stream_started=stream_started,
                        turn_count=next_turn,
                        retry_count=retry_count,
                    )
                try:
                    plan_request = PlanSubmission.model_validate(
                        plan_calls[0].get("args") or {}
                    )
                except Exception as exc:
                    return self._llm_outcome(
                        messages=message_dicts,
                        content=full_content,
                        error=f"submit_plan 参数无效: {exc}",
                        retryable=False,
                        stream_started=stream_started,
                        turn_count=next_turn,
                        retry_count=retry_count,
                    )
                return self._llm_outcome(
                    messages=message_dicts,
                    content=full_content,
                    route="plan_requested",
                    plan_request=plan_request.model_dump(mode="json"),
                    stream_started=stream_started,
                    turn_count=next_turn,
                    retry_count=retry_count,
                )
            if (
                full_content
                and next_turn == 1
                and parent_run_id is None
                and self._answer_stream is not None
            ):
                await self._answer_stream.append(full_content)
                await self._answer_stream.flush()
            return self._llm_outcome(
                messages=message_dicts,
                content=full_content,
                tool_calls=tool_calls,
                stream_started=stream_started,
                turn_count=next_turn,
                retry_count=retry_count,
            )
        except TimeoutError:
            error = f"LLM 流式调用超时（{self._harness_settings.llm_stream_timeout}s）"
            await self._emit(
                EventType.RUN_FAILED,
                {"call_id": llm_call_id, "error": error, "phase": "llm_call"},
                session_id,
                run_id,
            )
            await self._emit_llm_call_end(
                llm_call_id,
                status="failed",
                session_id=session_id,
                run_id=run_id,
            )
            if self._answer_stream is not None:
                await self._answer_stream.flush()
            next_retry = retry_count + 1
            return self._llm_outcome(
                messages=self._conversation_dicts(
                    compressed, system_prompt=system_prompt
                ),
                error=error,
                retryable=next_retry <= max_retries,
                stream_started=stream_started,
                turn_count=next_turn,
                retry_count=next_retry,
            )
        except Exception as exc:
            error = f"LLM 调用失败: {exc}"
            await self._emit(
                EventType.RUN_FAILED,
                {"call_id": llm_call_id, "error": str(exc), "phase": "llm_call"},
                session_id,
                run_id,
            )
            await self._emit_llm_call_end(
                llm_call_id,
                status="failed",
                session_id=session_id,
                run_id=run_id,
            )
            if self._answer_stream is not None:
                await self._answer_stream.flush()
            next_retry = retry_count + 1
            return self._llm_outcome(
                messages=self._conversation_dicts(
                    compressed, system_prompt=system_prompt
                ),
                error=error,
                retryable=next_retry <= max_retries,
                stream_started=stream_started,
                turn_count=next_turn,
                retry_count=next_retry,
            )

    async def execute_tool_batch(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        session_id: str,
        run_id: str,
        message_id: str | None,
        turn_count: int,
        stop_signal: asyncio.Event,
        parent_run_id: str | None = None,
        tool_names: list[str] | None = None,
    ) -> ToolBatchOutcome:
        """并行执行当前 LLM 响应中的工具调用批次。"""

        self._configure_tool_context(
            message_id=message_id,
            stop_signal=stop_signal,
            parent_run_id=parent_run_id,
            tool_names=tool_names,
        )
        await self._emit_thinking(
            EventType.THINKING_SUMMARY,
            "正在执行工具",
            session_id,
            run_id,
        )
        planned = [
            (
                f"{run_id}:tool-batch:{turn_count}:{index}",
                {
                    **call,
                    "id": call.get("id")
                    or self._stable_tool_call_id(run_id, turn_count, index, call),
                },
            )
            for index, call in enumerate(tool_calls)
        ]
        tool_messages = await self._execute_tool_calls(
            tool_calls=planned,
            session_id=session_id,
            run_id=run_id,
            tool_results_all=tool_results,
        )
        return ToolBatchOutcome(
            messages=[message_to_dict(item) for item in tool_messages],
            tool_results=tool_results,
            interrupted=self._should_stop(),
        )

    async def finish_execution(
        self,
        *,
        session_id: str,
        run_id: str,
        message_id: str | None,
        stop_signal: asyncio.Event,
        stream_version: int,
        stream_offset: int,
        turn_count: int,
        error: str | None,
        interrupted: bool,
        parent_run_id: str | None = None,
    ) -> None:
        """关闭流、发布终态事件并更新会话运行状态。"""

        self._configure_context(
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
            stop_signal=stop_signal,
            parent_run_id=parent_run_id,
            tool_names=None,
            stream_version=stream_version,
            stream_offset=stream_offset,
        )
        if self._answer_stream is not None:
            await self._answer_stream.flush(is_complete=True)
        await self._emit_thinking(
            EventType.THINKING_COMPLETED,
            "",
            session_id,
            run_id,
        )
        await self._emit(
            EventType.STREAM_END,
            {
                "run_id": run_id,
                "stream_id": self._answer_stream_id,
                "stream_type": "answer",
                "turn_count": turn_count,
                "error": error,
            },
            session_id,
            run_id,
        )
        if parent_run_id is None:
            await self._db.sessions.update(
                session_id,
                status="interrupted" if interrupted or self._should_stop() else "idle",
            )

    async def _persist_assistant_message(
        self,
        *,
        session_id: str,
        run_id: str,
        turn_count: int,
        content: str,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        message_id = f"{run_id}:assistant:{turn_count}"
        if await self._db.messages.get(message_id) is not None:
            return
        await self._db.messages.save(
            Message(
                id=message_id,
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=content,
                tool_calls=tool_calls,
                run_id=run_id,
                timestamp=datetime.now(),
            )
        )

    @staticmethod
    def _conversation_dicts(
        messages: Sequence[BaseMessage], *, system_prompt: str
    ) -> list[dict[str, Any]]:
        """去掉本轮临时注入的系统提示，只保存可恢复的会话消息。"""

        start = 0
        if (
            system_prompt
            and messages
            and isinstance(messages[0], SystemMessage)
            and str(messages[0].content) == system_prompt
        ):
            start = 1
        return [message_to_dict(item) for item in messages[start:]]
