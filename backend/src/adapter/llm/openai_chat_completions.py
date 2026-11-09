"""OpenAI 兼容 Chat Completions 流式协议适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast

import httpx2
import openai
from openai import AsyncOpenAI

from adapter.llm.openai_responses import (
    ResponseLimitError,
    _BoundedResponseTransport,
)
from domain.llm.model import (
    LlmCapabilityError,
    LlmError,
    LlmIncompleteError,
    LlmMessage,
    LlmProtocolError,
    LlmProviderError,
    LlmRefusalError,
    LlmRequest,
    LlmRequestError,
    LlmResponse,
    LlmRole,
    LlmStreamEvent,
    LlmText,
    LlmToolCall,
    LlmUsage,
    ResponseCompleted,
    TextDelta,
    ToolArgumentsDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.llm.ports import LlmStreamProvider

logger = logging.getLogger(__name__)
MAX_STREAM_CHUNKS = 8192
MAX_RAW_EVENT_BYTES = 1024 * 1024
MAX_TOTAL_RAW_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
MAX_TOOL_CALLS = 8


def _retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """解析 Retry-After 秒数或 HTTP 日期；无效值不提供重试建议。"""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            retry_time = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(tzinfo=UTC)
        current_time = now or datetime.now(UTC)
        seconds = (retry_time.astimezone(UTC) - current_time.astimezone(UTC)).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, 86_400.0)


def _json_value(value: Any) -> Any:
    """将 SDK 分片及轻量映射转换为可计量的 JSON 值。"""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: _json_value(item) for key, item in vars(value).items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise LlmProtocolError("llm_stream_event_invalid")


class OpenAIChatCompletionsAdapter(LlmStreamProvider):
    """实现受限、单次流式 Chat Completions 调用。"""

    def __init__(self) -> None:
        """初始化请求级客户端注册表和并发配额。"""
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._closed = False

    def stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AbstractAsyncContextManager[AsyncIterator[LlmStreamEvent]]:
        """使用 Run 固定的配置版本发起调用。"""
        return self._stream(runtime, request)

    async def complete(self, runtime: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
        """使用 runtime 配置执行无工具的单次文本请求。"""
        config = runtime.version
        if request.tools or request.tool_choice != "none":
            raise LlmRequestError("llm_compression_tools_unsupported")
        if not request.messages or len(request.messages) > 1024:
            raise LlmRequestError()
        messages = self._messages(request.messages)
        try:
            request_bytes = len(
                json.dumps(messages, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeError):
            raise LlmRequestError() from None
        if request_bytes > MAX_TOTAL_RAW_BYTES:
            raise LlmRequestError("llm_input_too_large")
        semaphore = self._semaphores.setdefault(
            config.id, asyncio.Semaphore(config.max_concurrency)
        )
        timeout = min(
            request.timeout_seconds or config.request_timeout_seconds,
            config.request_timeout_seconds,
        )
        acquired = False
        timeout_scope = asyncio.timeout(timeout)
        try:
            async with timeout_scope:
                await semaphore.acquire()
                acquired = True
                create = cast(Any, self._client(runtime).chat.completions.create)
                try:
                    response = await create(
                        model=config.model,
                        messages=messages,
                        tools=[],
                        tool_choice="none",
                        max_tokens=request.max_output_tokens or config.max_output_tokens,
                        stream=False,
                        **(
                            {"temperature": config.temperature}
                            if config.temperature is not None
                            else {}
                        ),
                    )
                except asyncio.CancelledError:
                    raise
                except LlmError:
                    raise
                except Exception as error:
                    self._raise_provider_error(error)
            try:
                response_bytes = len(
                    json.dumps(_json_value(response), ensure_ascii=False, allow_nan=False).encode(
                        "utf-8"
                    )
                )
            except (TypeError, ValueError, UnicodeError):
                raise LlmProtocolError("llm_response_invalid") from None
            if response_bytes > MAX_TOTAL_RAW_BYTES:
                raise LlmProtocolError("llm_response_too_large")
            choices = getattr(response, "choices", ())
            if not isinstance(choices, Sequence) or len(choices) != 1:
                raise LlmProtocolError("llm_response_invalid")
            choice = cast(Any, choices[0])
            finish_reason = getattr(choice, "finish_reason", None)
            message = choice.message
            if getattr(message, "refusal", None):
                raise LlmRefusalError("llm_refusal")
            if getattr(message, "tool_calls", None):
                raise LlmProtocolError("llm_compression_tool_output_unsupported")
            content = getattr(message, "content", None)
            if not isinstance(content, str) or not content.strip():
                raise LlmProtocolError("llm_compression_text_missing")
            if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
                raise LlmProtocolError("llm_response_too_large")
            if finish_reason == "length":
                raise LlmIncompleteError("llm_incomplete")
            if finish_reason != "stop":
                raise LlmProtocolError("llm_response_invalid")
            usage_item = getattr(response, "usage", None)
            usage = (
                None
                if usage_item is None
                else LlmUsage(
                    input_tokens=getattr(usage_item, "prompt_tokens", None),
                    output_tokens=getattr(usage_item, "completion_tokens", None),
                    total_tokens=getattr(usage_item, "total_tokens", None),
                )
            )
            return LlmResponse(
                output=(LlmText(content),),
                status="completed",
                incomplete_reason=None,
                model=getattr(response, "model", None) or config.model,
                response_id=getattr(response, "id", None),
                request_id=None,
                usage=usage,
            )
        except TimeoutError:
            if not timeout_scope.expired():
                raise
            raise LlmProviderError("llm_timeout") from None
        finally:
            if acquired:
                semaphore.release()

    @asynccontextmanager
    async def _stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
        """持有并发名额直到流结束、失败或调用方退出。"""
        config = runtime.version
        self._validate_capabilities(runtime, request)
        semaphore = self._semaphores.setdefault(
            config.id, asyncio.Semaphore(config.max_concurrency)
        )
        stream: Any = None
        timeout = min(
            request.timeout_seconds or config.request_timeout_seconds,
            config.request_timeout_seconds,
        )
        acquired = False
        timeout_scope = asyncio.timeout(timeout)
        try:
            try:
                async with timeout_scope:
                    await semaphore.acquire()
                    acquired = True
                    create = cast(Any, self._client(runtime).chat.completions.create)
                    try:
                        stream = await create(
                            model=config.model,
                            messages=self._messages(request.messages),
                            tools=self._tools(request),
                            tool_choice=self._tool_choice(request),
                            parallel_tool_calls=False,
                            max_tokens=request.max_output_tokens or config.max_output_tokens,
                            stream=True,
                            stream_options={"include_usage": True},
                            **(
                                {"temperature": config.temperature}
                                if config.temperature is not None
                                else {}
                            ),
                        )
                    except asyncio.CancelledError:
                        raise
                    except LlmError:
                        raise
                    except Exception as error:
                        self._raise_provider_error(error)
                    yield self._events(stream, runtime, request)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                if not timeout_scope.expired():
                    raise
                raise LlmProviderError("llm_timeout") from None
        finally:
            try:
                if stream is not None:
                    try:
                        await stream.close()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("llm_chat_stream_close_failed code=llm_cleanup_failed")
            finally:
                if acquired:
                    semaphore.release()

    @staticmethod
    def _validate_capabilities(runtime: RuntimeModelProfile, request: LlmRequest) -> None:
        """确保 Run 绑定版本支持所需的流式和澄清调用能力。"""
        if not runtime.version.supports_stream:
            raise LlmCapabilityError("llm_stream_unsupported")
        if not runtime.version.supports_tools:
            raise LlmCapabilityError("llm_tools_unsupported")

    async def aclose(self) -> None:
        """关闭本 Adapter 创建的所有 SDK 客户端。"""
        if self._closed:
            return
        self._closed = True
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:
                logger.warning("llm_chat_client_close_failed code=llm_cleanup_failed")
        self._clients.clear()

    def _client(self, runtime: RuntimeModelProfile) -> AsyncOpenAI:
        identity = (runtime.version.base_url, hashlib.sha256(runtime.api_key.encode()).hexdigest())
        client = self._clients.get(identity)
        if client is None:
            http_client = httpx2.AsyncClient(
                headers={"accept-encoding": "identity"},
                transport=_BoundedResponseTransport(
                    httpx2.AsyncHTTPTransport(),
                    MAX_TOTAL_RAW_BYTES,
                    event_limit=MAX_RAW_EVENT_BYTES,
                ),
            )
            client = AsyncOpenAI(
                api_key=runtime.api_key,
                base_url=runtime.version.base_url,
                max_retries=0,
                http_client=http_client,
            )
            self._clients[identity] = client
        return client

    @staticmethod
    def _messages(messages: tuple[LlmMessage, ...]) -> list[dict[str, Any]]:
        """将领域消息转换为 Chat Completions 消息序列。"""
        result: list[dict[str, Any]] = []
        pending_call_ids: set[str] = set()
        for message in messages:
            if message.continuation_items:
                raise LlmProtocolError("llm_chat_continuation_unsupported")
            role = message.role.value
            if message.tool_calls and role != LlmRole.ASSISTANT.value:
                raise LlmProtocolError("llm_chat_tool_calls_role_invalid")
            if message.tool_calls:
                call_ids = [call.call_id for call in message.tool_calls]
                if (
                    any(not call.call_id or not call.name for call in message.tool_calls)
                    or len(set(call_ids)) != len(call_ids)
                    or pending_call_ids.intersection(call_ids)
                ):
                    raise LlmProtocolError("llm_chat_tool_calls_invalid")
                result.append(
                    {
                        "role": "assistant",
                        "content": message.content or None,
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(
                                        _json_value(call.arguments),
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ),
                                },
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
                pending_call_ids.update(call_ids)
                continue
            if role == LlmRole.TOOL.value:
                if not message.tool_call_id or message.tool_call_id not in pending_call_ids:
                    raise LlmProtocolError("llm_chat_tool_result_orphaned")
                pending_call_ids.remove(message.tool_call_id)
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
            else:
                result.append({"role": role, "content": message.content})
        if pending_call_ids:
            raise LlmProtocolError("llm_chat_tool_result_missing")
        return result

    @staticmethod
    def _tools(request: LlmRequest) -> list[dict[str, Any]]:
        """构造每次请求独立的 function tool 快照。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.input_schema),
                },
            }
            for tool in request.tools
        ]

    @staticmethod
    def _tool_choice(request: LlmRequest) -> str | dict[str, Any]:
        """Map the domain choice to the actual Chat Completions wire value."""
        if request.tool_choice in {"auto", "none", "required"}:
            return request.tool_choice
        if request.tool_choice.startswith("tool:"):
            name = request.tool_choice[5:]
            if not name:
                raise LlmRequestError("llm_tool_choice_invalid")
            return {"type": "function", "function": {"name": name}}
        raise LlmRequestError("llm_tool_choice_invalid")

    async def _events(
        self, stream: Any, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AsyncIterator[LlmStreamEvent]:
        """重组分片调用，等待结束 chunk 后才发出完整结果。"""
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        usage: LlmUsage | None = None
        response_id: str | None = None
        total_raw_bytes = 0
        text_bytes = 0
        argument_bytes = 0
        chunk_count = 0
        iterator = aiter(stream)
        while True:
            try:
                chunk = await anext(iterator)
            except StopAsyncIteration:
                break
            except asyncio.CancelledError:
                raise
            except LlmError:
                raise
            except Exception as error:
                self._raise_provider_error(error)
            chunk_count += 1
            if chunk_count > MAX_STREAM_CHUNKS:
                raise LlmProtocolError("llm_stream_too_large")
            try:
                chunk_bytes = len(
                    json.dumps(_json_value(chunk), ensure_ascii=False, allow_nan=False).encode(
                        "utf-8"
                    )
                )
            except (TypeError, ValueError, UnicodeError):
                raise LlmProtocolError("llm_stream_event_invalid") from None
            if chunk_bytes > MAX_RAW_EVENT_BYTES:
                raise LlmProtocolError("llm_stream_event_too_large")
            total_raw_bytes += chunk_bytes
            if total_raw_bytes > MAX_TOTAL_RAW_BYTES:
                raise LlmProtocolError("llm_stream_too_large")
            response_id = getattr(chunk, "id", None) or response_id
            if getattr(chunk, "usage", None) is not None:
                item = chunk.usage
                usage = LlmUsage(
                    input_tokens=getattr(item, "prompt_tokens", None),
                    output_tokens=getattr(item, "completion_tokens", None),
                    total_tokens=getattr(item, "total_tokens", None),
                )
            choices = getattr(chunk, "choices", ())
            for choice in choices:
                if choice.finish_reason is not None:
                    if finish_reason is not None and finish_reason != choice.finish_reason:
                        raise LlmProtocolError()
                    finish_reason = choice.finish_reason
                delta = choice.delta
                refusal = getattr(delta, "refusal", None)
                if refusal:
                    raise LlmRefusalError("llm_refusal")
                content = getattr(delta, "content", None)
                if content:
                    if not isinstance(content, str):
                        raise LlmProtocolError()
                    text_bytes += len(content.encode("utf-8"))
                    if text_bytes > MAX_TEXT_BYTES:
                        raise LlmProtocolError("llm_response_too_large")
                    text_parts.append(content)
                    yield TextDelta(output_index=0, content_index=0, text=content)
                for fragment in getattr(delta, "tool_calls", None) or ():
                    index = fragment.index
                    state = calls.get(index)
                    if state is None:
                        if len(calls) >= MAX_TOOL_CALLS:
                            raise LlmProtocolError("llm_stream_too_large")
                        state = {"id": "", "name": "", "arguments": ""}
                        calls[index] = state
                    function = fragment.function
                    call_id = getattr(fragment, "id", None)
                    name = getattr(function, "name", None)
                    if call_id:
                        if state["id"] and state["id"] != call_id:
                            raise LlmProtocolError("llm_tool_call_identity_invalid")
                        state["id"] = call_id
                    if name:
                        if state["name"] and state["name"] != name:
                            raise LlmProtocolError("llm_tool_call_identity_invalid")
                        state["name"] = name
                    if call_id or name:
                        yield ToolCallStarted(
                            output_index=index, call_id=state["id"], name=state["name"]
                        )
                    arguments = getattr(function, "arguments", None)
                    if arguments:
                        if not isinstance(arguments, str):
                            raise LlmProtocolError("llm_tool_arguments_invalid")
                        argument_bytes += len(arguments.encode("utf-8"))
                        if argument_bytes > MAX_TOOL_ARGUMENT_BYTES:
                            raise LlmProtocolError("llm_tool_arguments_too_large")
                        state["arguments"] += arguments
                        yield ToolArgumentsDelta(
                            output_index=index, call_id=state["id"], delta=arguments
                        )
        if finish_reason not in {"stop", "tool_calls"}:
            raise LlmIncompleteError("llm_incomplete")
        output: list[LlmText | LlmToolCall] = []
        if text_parts:
            output.append(LlmText("".join(text_parts)))
        if calls and finish_reason != "tool_calls":
            raise LlmProtocolError()
        call_ids = [state["id"] for state in calls.values()]
        if len(set(call_ids)) != len(call_ids):
            raise LlmProtocolError("llm_tool_call_identity_invalid")
        for index in sorted(calls):
            state = calls[index]
            try:
                arguments = json.loads(state["arguments"])
            except (TypeError, ValueError):
                raise LlmProtocolError("llm_tool_arguments_invalid") from None
            if not state["id"] or not state["name"] or not isinstance(arguments, dict):
                raise LlmProtocolError("llm_tool_arguments_invalid")
            call = LlmToolCall(state["id"], state["name"], arguments)
            output.append(call)
            yield ToolCallCompleted(output_index=index, tool_call=call)
        result = LlmResponse(
            output=tuple(output),
            status="completed",
            incomplete_reason=None,
            model=runtime.version.model,
            response_id=response_id,
            request_id=None,
            usage=usage,
        )
        yield ResponseCompleted(result)

    @staticmethod
    def _raise_provider_error(error: Exception) -> None:
        """映射供应商异常为固定安全错误码。"""
        if isinstance(error, ResponseLimitError):
            raise LlmProtocolError(str(error)) from None
        if isinstance(error, openai.APIConnectionError):
            current: BaseException | None = error
            visited: set[int] = set()
            while current is not None and id(current) not in visited:
                if isinstance(current, ResponseLimitError):
                    raise LlmProtocolError(str(current)) from None
                visited.add(id(current))
                current = current.__cause__ or current.__context__
        if isinstance(error, openai.RateLimitError):
            retry_after = _retry_after_seconds(error.response.headers.get("retry-after"))
            raise LlmProviderError(
                "llm_rate_limited", retry_after_seconds=retry_after, retry_safe=True
            ) from None
        if isinstance(error, openai.AuthenticationError):
            raise LlmProviderError("llm_authentication_failed") from None
        if isinstance(error, openai.APITimeoutError):
            raise LlmProviderError("llm_timeout") from None
        if isinstance(error, openai.APIConnectionError):
            raise LlmProviderError("llm_connection_failed") from None
        if isinstance(error, openai.APIStatusError) and error.status_code >= 500:
            raise LlmProviderError("llm_temporary_upstream_error", retry_safe=True) from None
        if isinstance(error, openai.APIStatusError) and error.status_code == 400:
            body = error.body
            if isinstance(body, Mapping):
                provider_error = body.get("error")
                if isinstance(provider_error, Mapping) and provider_error.get("code") in {
                    "context_length_exceeded",
                    "max_tokens_exceeded",
                }:
                    raise LlmRequestError("llm_context_window_exceeded") from None
        raise LlmProviderError("llm_provider_error") from None


__all__ = ["OpenAIChatCompletionsAdapter"]
