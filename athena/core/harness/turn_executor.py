"""LangGraph execution-loop 的单轮 LLM 和工具执行器。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.tools import StructuredTool

from athena.contracts.events import EventType
from athena.contracts.errors import ExecutionError
from athena.core.harness.execution_support import ExecutionSupport
from athena.models import Message, MessageRole
from athena.utils.llm_response import extract_message_text
from athena.utils.message_conversion import dict_to_message, message_to_dict
from athena.runtime.stream_coalescer import StreamCoalescer
from athena.contracts.orchestration import (
    PLAN_SUBMISSION_TOOL_NAME,
    PlanSubmission,
)
from athena.contracts.tool_policy import DELEGATION_TOOL_NAMES
from athena.observability.langsmith import finish_span, trace_span


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
    error_detail: dict[str, Any] | None = None
    llm_result_status: str = "completed"
    llm_retry_reason: str = "none"
    retry_feedback: str | None = None
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
class ToolCallOutcome:
    """单个工具调用节点的 JSON 边界结果。"""

    # messages 只包含本批次新增的 ToolMessage，由执行节点追加到已有会话上下文。
    messages: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    interrupted: bool = False


@dataclass(frozen=True, slots=True)
class _LlmTurnContext:
    """单次 LLM 调用内部使用的上下文数据。"""

    session_id: str
    run_id: str
    system_prompt: str
    turn_count: int
    retry_count: int
    max_turns: int
    max_retries: int
    parent_run_id: str | None
    stream_started: bool
    compressed: list[BaseMessage]
    llm_call_id: str
    started_at: float
    next_turn: int

    @property
    def duration_ms(self) -> float:
        """返回当前调用从开始到读取该属性时经过的毫秒数。"""

        return (time.time() - self.started_at) * 1000


class TurnExecutor(ExecutionSupport):
    """执行一次 LLM 调用、一次工具批次或一次执行收尾。"""

    def __init__(self, *, compressor, **kwargs: Any) -> None:
        """绑定单轮 LLM 所需的上下文压缩器。"""
        super().__init__(**kwargs)
        self._compressor = compressor

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
            publish_realtime=self._event_publisher.publish_realtime,
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
        await self._emit_thinking(
            EventType.THINKING_STARTED,
            "正在准备请求",
            session_id,
            run_id,
        )
        return stream_started

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
        error_detail: ExecutionError | dict[str, Any] | None = None,
        interrupted: bool = False,
        retryable: bool = False,
        route: str = "agent_loop",
        plan_request: dict[str, Any] | None = None,
        llm_result_status: str = "completed",
        llm_retry_reason: str = "none",
        retry_feedback: str | None = None,
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
            error_detail=(
                error_detail.model_dump(mode="json")
                if isinstance(error_detail, ExecutionError)
                else error_detail
            ),
            llm_result_status=llm_result_status,
            llm_retry_reason=llm_retry_reason,
            retry_feedback=retry_feedback,
            interrupted=interrupted,
            retryable=retryable,
            turn_count=turn_count,
            retry_count=retry_count,
            stream_started=stream_started,
            stream_version=self._answer_stream.version,
            stream_offset=self._answer_stream.offset,
            route=route,
            plan_request=plan_request,
        )

    @staticmethod
    def _stable_tool_call_id(
        run_id: str, turn_count: int, index: int, tool_call: dict[str, Any]
    ) -> str:
        raw = f"{run_id}:{turn_count}:{index}:{tool_call.get('name', '')}:{tool_call.get('args', {})}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        return f"{run_id}:tool:{turn_count}:{index}:{digest}"

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
        retry_feedback: str | None = None,
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

        compressed = await self._compress_messages(
            messages,
            session_id=session_id,
            run_id=run_id,
            system_prompt=system_prompt,
            retry_feedback=retry_feedback,
        )

        context = _LlmTurnContext(
            session_id=session_id,
            run_id=run_id,
            system_prompt=system_prompt,
            turn_count=turn_count,
            retry_count=retry_count,
            max_turns=max_turns,
            max_retries=max_retries,
            parent_run_id=parent_run_id,
            stream_started=stream_started,
            compressed=compressed,
            llm_call_id=f"{run_id}:llm:{turn_count + 1}",
            started_at=time.time(),
            next_turn=turn_count + 1,
        )

        budget_outcome = await self._turn_budget_outcome(context)
        if budget_outcome is not None:
            return budget_outcome

        await self._emit_llm_call_started(context)
        try:
            content, tool_calls = await self._collect_llm_response(context, tool_names)

            if self._should_stop():
                return await self._interrupted_llm_outcome(context)
            if not content.strip() and not tool_calls:
                return await self._empty_llm_outcome(context)
            invalid_tool_call = next(
                (
                    call
                    for call in tool_calls
                    if not call.get("name") or not isinstance(call.get("args"), dict)
                ),
                None,
            )
            if invalid_tool_call is not None:
                return await self._failed_llm_outcome(
                    context,
                    "LLM 返回的工具调用缺少有效名称或参数对象",
                    failure_detail=str(invalid_tool_call),
                    business_reason="invalid_tool_call",
                    feedback=(
                        "上一次工具调用格式无效。每个工具调用都必须包含有效工具名，"
                        "并且 args 必须是 JSON 对象。请重新生成。"
                    ),
                )
            return await self._complete_llm_outcome(context, content, tool_calls)
        except TimeoutError:
            return await self._failed_llm_outcome(
                context,
                f"LLM 调用超时（{self._llm_timeout}s）",
                failure_exception=TimeoutError("LLM stream timeout"),
            )
        except Exception as exc:
            return await self._failed_llm_outcome(
                context,
                f"LLM 调用失败: {exc}",
                failure_detail=str(exc),
                failure_exception=exc,
            )

    async def _compress_messages(
        self,
        messages: Sequence[Message | dict[str, Any]],
        *,
        session_id: str,
        run_id: str,
        system_prompt: str,
        retry_feedback: str | None = None,
    ) -> list[BaseMessage]:
        """将 checkpoint 消息压缩为可发送给 LLM 的消息列表。"""

        domain_messages = _to_domain_messages(
            messages, session_id=session_id, run_id=run_id
        )
        compressed_domain = await self._compressor.compress(
            domain_messages, session_id=session_id
        )
        result: list[BaseMessage] = []
        if system_prompt:
            result.append(SystemMessage(content=system_prompt))
        if retry_feedback:
            result.append(SystemMessage(content=retry_feedback))
        result.extend(dict_to_message(message) for message in compressed_domain)
        return result

    async def _collect_llm_response(
        self, context: _LlmTurnContext, tool_names: list[str] | None
    ) -> tuple[str, list[dict[str, Any]]]:
        """调用一次完整 LLM 响应；技术重试由 Provider 层负责。"""

        bound_tools = self._get_llm_tools(
            tool_names=tool_names, parent_run_id=context.parent_run_id
        )
        bound_llm = self._llm.bind_tools(bound_tools) if bound_tools else self._llm
        async with trace_span(
            name=f"llm.turn.{context.next_turn}",
            run_type="llm",
            inputs={"messages": [m.model_dump(mode="json") for m in context.compressed]},
            run_id=f"{context.run_id}:llm:{context.llm_call_id}",
            metadata={
                "session_id": context.session_id,
                "run_id": context.run_id,
                "turn": context.next_turn,
            },
            tags=["athena", "llm", "invoke"],
        ) as trace:
            try:
                async with asyncio.timeout(self._llm_timeout):
                    response = await bound_llm.ainvoke(context.compressed)
                finish_span(trace, outputs={"status": "success"})
            except Exception as exc:
                finish_span(trace, error=str(exc))
                raise

        content = extract_message_text(response)
        raw_tool_calls = getattr(response, "tool_calls", None) or []
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
                    context.run_id, context.next_turn, index, tool_call
                )
            tool_calls.append(tool_call)
        return content, tool_calls

    async def _turn_budget_outcome(
        self, context: _LlmTurnContext
    ) -> LlmTurnOutcome | None:
        """检查轮次预算；超限时发布事件并返回失败结果，否则返回 None。"""

        if context.next_turn <= context.max_turns:
            return None
        error = f"轮次预算超限: {context.next_turn}/{context.max_turns}"
        await self._emit(
            EventType.BUDGET_EXCEEDED,
            {"reason": error, "turn_count": context.turn_count},
            context.session_id,
            context.run_id,
        )
        return self._llm_outcome(
            messages=self._conversation_dicts(
                context.compressed, system_prompt=context.system_prompt
            ),
            error=error,
            error_detail=ExecutionError(
                code="budget_exceeded",
                message=error,
                error_type="BudgetExceeded",
                retryable=False,
                phase="llm_call",
            ),
            stream_started=context.stream_started,
            turn_count=context.turn_count,
            retry_count=context.retry_count,
        )

    async def _emit_llm_call_started(self, context: _LlmTurnContext) -> None:
        """发布单次 LLM 调用开始事件和状态提示。"""

        await self._emit(
            EventType.LLM_CALL_START,
            {"call_id": context.llm_call_id, "turn": context.next_turn},
            context.session_id,
            context.run_id,
        )
        await self._emit_thinking(
            EventType.THINKING_SUMMARY,
            "正在生成回答",
            context.session_id,
            context.run_id,
        )

    async def _interrupted_llm_outcome(
        self, context: _LlmTurnContext
    ) -> LlmTurnOutcome:
        """构造用户中断后的可恢复执行结果。"""

        await self._emit_llm_call_end(
            context.llm_call_id,
            status="failed",
            session_id=context.session_id,
            run_id=context.run_id,
        )
        if self._answer_stream is not None:
            await self._answer_stream.flush()
        return self._llm_outcome(
            messages=self._conversation_dicts(
                context.compressed, system_prompt=context.system_prompt
            ),
            interrupted=True,
            error_detail=ExecutionError(
                code="run_interrupted",
                message="运行被用户中断",
                error_type="CancelledError",
                retryable=True,
                phase="llm_call",
            ),
            stream_started=context.stream_started,
            turn_count=context.turn_count,
            retry_count=context.retry_count,
        )

    async def _empty_llm_outcome(self, context: _LlmTurnContext) -> LlmTurnOutcome:
        """构造空响应失败结果，并保留下一次重试机会。"""

        error = "LLM 返回空响应（无内容且无工具调用）"
        return await self._failed_llm_outcome(context, error)

    async def _failed_llm_outcome(
        self,
        context: _LlmTurnContext,
        error: str,
        *,
        failure_detail: str | None = None,
        failure_exception: BaseException | None = None,
        business_reason: str | None = None,
        feedback: str | None = None,
    ) -> LlmTurnOutcome:
        """发布失败事件并构造按重试预算可重试的结果。"""

        semantic_failure = failure_exception is None
        next_retry = context.retry_count + 1 if semantic_failure else context.retry_count
        retryable = semantic_failure and next_retry <= context.max_retries
        result_status = "semantic_retry" if semantic_failure else "technical_failure"
        retry_reason = business_reason or (
            "empty_response" if "空响应" in error else "provider_exhausted"
        )
        feedback = (
            feedback
            or "上一次响应为空，请直接生成有效的最终回答或合法的工具调用。"
            if semantic_failure
            else None
        )
        detail = (
            ExecutionError.from_exception(
                failure_exception,
                code=(
                    "llm_timeout"
                    if isinstance(failure_exception, TimeoutError)
                    else "llm_call_failed"
                ),
                retryable=False,
                phase="llm_call",
            )
            if failure_exception is not None
            else ExecutionError.from_legacy_value(
                failure_detail or error,
                code="llm_empty_response" if "空响应" in error else "llm_call_failed",
                retryable=retryable,
                phase="llm_call",
            )
        )
        event_type = EventType.LLM_VALIDATION_FAILED if semantic_failure else EventType.RUN_FAILED
        await self._emit(
            event_type,
            {
                "call_id": context.llm_call_id,
                "error": failure_detail or error,
                "error_detail": detail.model_dump(mode="json"),
                "phase": "llm_call",
                "retryable": retryable,
            },
            context.session_id,
            context.run_id,
        )
        await self._emit_llm_call_end(
            context.llm_call_id,
            status="failed",
            session_id=context.session_id,
            run_id=context.run_id,
        )
        if self._answer_stream is not None:
            await self._answer_stream.flush()
        return self._llm_outcome(
            messages=self._conversation_dicts(
                context.compressed, system_prompt=context.system_prompt
            ),
            error=error,
            error_detail=detail,
            retryable=retryable,
            llm_result_status=result_status,
            llm_retry_reason=retry_reason,
            retry_feedback=feedback,
            stream_started=context.stream_started,
            turn_count=context.next_turn,
            retry_count=next_retry,
        )

    async def _complete_llm_outcome(
        self,
        context: _LlmTurnContext,
        content: str,
        tool_calls: list[dict[str, Any]],
    ) -> LlmTurnOutcome:
        """持久化成功响应、结束调用事件，并选择下一图节点路由。"""

        message_dicts = self._conversation_dicts(
            context.compressed, system_prompt=context.system_prompt
        )
        message_dicts.append(
            message_to_dict(AIMessage(content=content, tool_calls=tool_calls))
        )
        await self._persist_assistant_message(
            session_id=context.session_id,
            run_id=context.run_id,
            turn_count=context.next_turn,
            content=content,
            tool_calls=tool_calls,
        )
        await self._emit_llm_call_end(
            context.llm_call_id,
            status="completed",
            session_id=context.session_id,
            run_id=context.run_id,
            duration_ms=context.duration_ms,
            tool_calls_count=len(tool_calls),
            tool_calls=tool_calls,
        )
        if self._answer_stream is not None:
            await self._answer_stream.flush()
        plan_calls = [
            call for call in tool_calls if call.get("name") == PLAN_SUBMISSION_TOOL_NAME
        ]
        if plan_calls:
            return self._plan_outcome(
                context,
                message_dicts=message_dicts,
                content=content,
                plan_calls=plan_calls,
                tool_calls=tool_calls,
            )
        if (
            content
            and not tool_calls
            and context.parent_run_id is None
            and self._answer_stream is not None
        ):
            if not context.stream_started:
                await self._emit(
                    EventType.STREAM_START,
                    {
                        "run_id": context.run_id,
                        "stream_id": self._answer_stream_id,
                        "stream_type": "answer",
                    },
                    context.session_id,
                    context.run_id,
                )
            await self._answer_stream.append(content)
            await self._answer_stream.flush()
        return self._llm_outcome(
            messages=message_dicts,
            content=content,
            tool_calls=tool_calls,
            retry_feedback=None,
            stream_started=bool(content and not tool_calls)
            or context.stream_started,
            turn_count=context.next_turn,
            retry_count=context.retry_count,
        )

    def _plan_outcome(
        self,
        context: _LlmTurnContext,
        *,
        message_dicts: list[dict[str, Any]],
        content: str,
        plan_calls: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
    ) -> LlmTurnOutcome:
        """校验计划提交协议并构造计划请求或协议错误结果。"""

        if context.parent_run_id is not None:
            next_retry = context.retry_count + 1
            retryable = next_retry <= context.max_retries
            return self._llm_outcome(
                messages=message_dicts,
                content=content,
                error="submit_plan 只能由顶层 Agent 调用",
                error_detail=ExecutionError(
                    code="invalid_plan_submission",
                    message="submit_plan 只能由顶层 Agent 调用",
                    error_type="PlanProtocolError",
                    retryable=retryable,
                    phase="llm_call",
                ),
                retryable=retryable,
                llm_result_status="semantic_retry",
                llm_retry_reason="invalid_plan_submission",
                retry_feedback=(
                    "上一次调用了仅允许顶层 Agent 使用的 submit_plan。"
                    "请改用可用工具，或直接回答。"
                ),
                stream_started=context.stream_started,
                turn_count=context.next_turn,
                retry_count=next_retry,
            )
        if len(plan_calls) != 1 or len(plan_calls) != len(tool_calls):
            next_retry = context.retry_count + 1
            retryable = next_retry <= context.max_retries
            return self._llm_outcome(
                messages=message_dicts,
                content=content,
                error="submit_plan 不能与普通工具调用混用",
                error_detail=ExecutionError(
                    code="invalid_plan_submission",
                    message="submit_plan 不能与普通工具调用混用",
                    error_type="PlanProtocolError",
                    retryable=retryable,
                    phase="llm_call",
                ),
                retryable=retryable,
                llm_result_status="semantic_retry",
                llm_retry_reason="invalid_plan_submission",
                retry_feedback=(
                    "上一次把 submit_plan 与普通工具调用混用。"
                    "请单独提交计划，或只调用普通工具。"
                ),
                stream_started=context.stream_started,
                turn_count=context.next_turn,
                retry_count=next_retry,
            )
        try:
            plan_request = PlanSubmission.model_validate(
                plan_calls[0].get("args") or {}
            )
        except Exception as exc:
            next_retry = context.retry_count + 1
            retryable = next_retry <= context.max_retries
            return self._llm_outcome(
                messages=message_dicts,
                content=content,
                error=f"submit_plan 参数无效: {exc}",
                error_detail=ExecutionError.from_exception(
                    exc,
                    code="invalid_plan_submission",
                    retryable=retryable,
                    phase="llm_call",
                ),
                retryable=retryable,
                llm_result_status="semantic_retry",
                llm_retry_reason="invalid_plan_submission",
                retry_feedback=f"上一次 submit_plan 参数无效：{exc}。"
                "请重新生成符合 schema 的计划。",
                stream_started=context.stream_started,
                turn_count=context.next_turn,
                retry_count=next_retry,
            )
        return self._llm_outcome(
            messages=message_dicts,
            content=content,
            route="plan_requested",
            plan_request=plan_request.model_dump(mode="json"),
            stream_started=context.stream_started,
            turn_count=context.next_turn,
            retry_count=context.retry_count,
        )

    async def execute_tool_call(
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
        approval_decisions: dict[str, str] | None = None,
    ) -> ToolCallOutcome:
        """执行当前 Send 分支中的单个工具调用。"""

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
            turn_count=turn_count,
            tool_results_all=tool_results,
            approval_decisions=approval_decisions,
        )
        return ToolCallOutcome(
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
        error_detail: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
        content: str = "",
        stream_started: bool = False,
    ) -> None:
        """关闭流、发布带完整内容的终态事件并更新会话运行状态。"""

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
        if stream_started:
            await self._emit(
                EventType.STREAM_END,
                {
                    "run_id": run_id,
                    "stream_id": self._answer_stream_id,
                    "stream_type": "answer",
                    "turn_count": turn_count,
                    "content": content,
                    "error": error,
                    "error_detail": error_detail,
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

        return [
            message_to_dict(item)
            for item in messages
            if not isinstance(item, SystemMessage)
        ]
