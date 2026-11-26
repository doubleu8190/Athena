"""OpenAI 兼容 Chat Completions 流式协议适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import httpx2
import openai
from openai import AsyncOpenAI

from domain.llm.model import (
    LlmCapabilityError,
    LlmError,
    LlmImagePart,
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
    LlmTextPart,
    LlmToolCall,
    LlmUsage,
    ModelProfileVersion,
    ResponseCompleted,
    TextDelta,
    ToolArgumentsDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.llm.ports import LlmRuntimeTextProvider, LlmStreamProvider
from infrastructure.http.retry import parse_retry_after_seconds

logger = logging.getLogger(__name__)
MAX_STREAM_CHUNKS = 65536
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
MAX_TOOL_CALLS = 8
_ALLOWED_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_ALLOWED_IMAGE_DETAILS = frozenset({"auto", "low", "high"})


@dataclass(slots=True)
class _ToolCallAccumulator:
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    argument_bytes: int = 0


def _retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """解析 Retry-After 秒数或 HTTP 日期；无效值不提供重试建议。"""
    return parse_retry_after_seconds(value, now=now)


def _json_value(value: Any) -> Any:
    """将工具调用参数中的只读映射或嵌套序列转为标准 JSON 可序列化对象。"""
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise LlmProtocolError("llm_stream_event_invalid")


def _plain_text_content(message: LlmMessage) -> str:
    """提取 assistant（含工具调用）或 tool 消息的纯文本内容并校验不包含非文本部件。"""
    text = message.content
    if not message.content_parts:
        return text
    extra_parts: list[str] = []
    for part in message.content_parts:
        if not isinstance(part, LlmTextPart) or not isinstance(part.text, str):
            raise LlmProtocolError("llm_chat_image_invalid")
        extra_parts.append(part.text)
    return text + "".join(extra_parts)


class OpenAIChatCompletionsAdapter(LlmStreamProvider, LlmRuntimeTextProvider):
    """实现受限、单次流式 Chat Completions 调用。"""

    def __init__(self) -> None:
        """初始化请求级客户端注册表和并发配额。"""
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._closed = False

    @staticmethod
    def _request_timeout(config: ModelProfileVersion, request: LlmRequest) -> float:
        """计算单次请求超时，同时不得超过模型版本的硬上限。"""
        return min(
            request.timeout_seconds or config.request_timeout_seconds,
            config.request_timeout_seconds,
        )

    def _semaphore_for(self, config: ModelProfileVersion) -> asyncio.Semaphore:
        """取得指定模型版本共享的并发配额。"""
        return self._semaphores.setdefault(config.id, asyncio.Semaphore(config.max_concurrency))

    async def complete(self, runtime: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
        """使用 runtime 配置执行无工具的单次文本请求。"""
        config = runtime.version
        # 摘要压缩、自动标题与安全停止报告均属于纯文本路径，严禁误发任何图片部件。
        if request.has_image_parts():
            raise LlmProtocolError("llm_compression_image_unsupported")
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
        if request_bytes > MAX_INPUT_BYTES:
            raise LlmRequestError("llm_input_too_large")
        semaphore = self._semaphore_for(config)
        timeout = self._request_timeout(config, request)
        request_parameters = self._complete_request_parameters(
            config, messages, request, timeout=timeout
        )
        acquired = False
        timeout_scope = asyncio.timeout(timeout)
        try:
            async with timeout_scope:
                await semaphore.acquire()
                acquired = True
                create = cast(Any, self._client(runtime).chat.completions.create)
                try:
                    response = await create(**request_parameters)
                except asyncio.CancelledError:
                    raise
                except LlmError:
                    raise
                except Exception as error:
                    self._raise_provider_error(error)
            return self._complete_response(response, config)
        except TimeoutError:
            if not timeout_scope.expired():
                raise
            raise LlmProviderError("llm_timeout") from None
        finally:
            if acquired:
                semaphore.release()

    @asynccontextmanager
    async def stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
        """使用 Run 固定的配置版本发起流式调用，并持有并发名额直到流结束、失败或调用方退出。"""
        config = runtime.version
        # 先确认绑定版本支持流式、工具及（若携带图片）视觉能力，再申请并发名额。
        self._validate_capabilities(runtime, request)
        semaphore = self._semaphore_for(config)
        stream: Any = None
        timeout = self._request_timeout(config, request)
        acquired = False
        timeout_scope = asyncio.timeout(timeout)
        try:
            try:
                async with timeout_scope:
                    await semaphore.acquire()
                    acquired = True
                    async_client = self._client(runtime)
                    try:
                        # 只发起一次流式请求（SDK 重试已禁用），重试策略由执行器负责。
                        request_parameters = self._stream_request_parameters(
                            config, request, timeout=timeout
                        )
                        stream = await async_client.chat.completions.create(**request_parameters)
                    except asyncio.CancelledError:
                        raise
                    except LlmError:
                        raise
                    except Exception as error:
                        self._raise_provider_error(error)
                    # 事件转换器负责分片重组、文本/工具体积上限与协议校验。
                    yield self._events(stream, runtime)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                if not timeout_scope.expired():
                    raise
                raise LlmProviderError("llm_timeout") from None
        finally:
            # 无论正常结束、报错还是调用方提前退出，都关闭上游流并释放并发名额。
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
    def _validate_capabilities(
        runtime: RuntimeModelProfile,
        request: LlmRequest | None = None,
    ) -> None:
        """确保 Run 绑定版本支持所需的流式、工具及按需视觉理解能力。"""
        if not runtime.version.supports_stream:
            raise LlmCapabilityError("llm_stream_unsupported")
        if not runtime.version.supports_tools:
            raise LlmCapabilityError("llm_tools_unsupported")
        if (
            request is not None
            and request.has_image_parts()
            and not runtime.version.supports_vision
        ):
            raise LlmCapabilityError("llm_vision_unsupported")

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
            # 使用标准 httpx2 异步客户端（禁重定向、有界连接池、允许常规 gzip 协商）；
            # 流式文本、工具参数、分片数上限均在应用层强制校验。
            http_client = httpx2.AsyncClient(
                follow_redirects=False,
                timeout=httpx2.Timeout(30.0, connect=10.0),
                limits=httpx2.Limits(
                    max_connections=32,
                    max_keepalive_connections=16,
                    keepalive_expiry=30.0,
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
    def _common_request_parameters(
        config: ModelProfileVersion,
        messages: list[dict[str, Any]],
        request: LlmRequest,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """构造非流式与流式入口共用的 Chat Completions 参数。"""
        parameters: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens or config.max_output_tokens,
            # 显式覆盖 SDK 默认 600 秒，使 HTTP 超时与本次 Run/Profile deadline 一致。
            "timeout": timeout,
        }
        if config.temperature is not None:
            parameters["temperature"] = config.temperature
        return parameters

    @classmethod
    def _complete_request_parameters(
        cls,
        config: ModelProfileVersion,
        messages: list[dict[str, Any]],
        request: LlmRequest,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """构造无工具、非流式的完整响应请求参数（无工具时不发送空 tools/tool_choice）。"""
        return {
            **cls._common_request_parameters(config, messages, request, timeout=timeout),
            "stream": False,
        }

    def _stream_request_parameters(
        self,
        config: ModelProfileVersion,
        request: LlmRequest,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """构造携带工具配置与 usage 尾分片的流式请求参数。"""
        parameters: dict[str, Any] = {
            **self._common_request_parameters(
                config, self._messages(request.messages), request, timeout=timeout
            ),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        tool_choice = self._tool_choice(request)
        tools = self._tools(request)
        # 仅在提供至少一个工具定义时才发送 tools、tool_choice 与 parallel_tool_calls。
        if tools:
            parameters["tools"] = tools
            parameters["tool_choice"] = tool_choice
            parameters["parallel_tool_calls"] = config.supports_parallel_tool_calls
        return parameters

    @staticmethod
    def _usage(raw_usage: Any) -> LlmUsage | None:
        """将 SDK usage 转为领域用量；缺失用量保持为未知。"""
        if raw_usage is None:
            return None
        return LlmUsage(
            input_tokens=getattr(raw_usage, "prompt_tokens", None),
            output_tokens=getattr(raw_usage, "completion_tokens", None),
            total_tokens=getattr(raw_usage, "total_tokens", None),
        )

    def _complete_response(self, response: Any, config: ModelProfileVersion) -> LlmResponse:
        """校验非流式响应并转换为唯一文本结果（强制 1 MiB 文本上限）。"""
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
        return LlmResponse(
            output=(LlmText(content),),
            status="completed",
            incomplete_reason=None,
            model=getattr(response, "model", None) or config.model,
            response_id=getattr(response, "id", None),
            request_id=None,
            usage=self._usage(getattr(response, "usage", None)),
        )

    @classmethod
    def _serialize_content_parts(cls, message: LlmMessage) -> str | list[dict[str, Any]]:
        """将非空 content_parts 组装为标准 OpenAI Chat Completions 多模态 content 数组。"""
        parts: list[dict[str, Any]] = []
        if message.content:
            parts.append({"type": "text", "text": message.content})
        for part in message.content_parts:
            if isinstance(part, LlmTextPart):
                if not isinstance(part.text, str):
                    raise LlmProtocolError("llm_chat_image_invalid")
                if part.text:
                    parts.append({"type": "text", "text": part.text})
            elif isinstance(part, LlmImagePart):
                # 严格校验 MIME 白名单、无空白 ASCII base64 与 detail 枚举，绝不回显 data_base64。
                data_base64 = part.data_base64
                if (
                    part.mime_type not in _ALLOWED_IMAGE_MIME_TYPES
                    or not isinstance(data_base64, str)
                    or not data_base64
                    or not data_base64.isascii()
                    or data_base64 != data_base64.strip()
                    or len(data_base64.split(None, 1)) != 1
                    or part.detail not in _ALLOWED_IMAGE_DETAILS
                ):
                    raise LlmProtocolError("llm_chat_image_invalid")
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{part.mime_type};base64,{part.data_base64}",
                            "detail": part.detail,
                        },
                    }
                )
            else:
                raise LlmProtocolError("llm_chat_image_invalid")
        return parts if parts else ""

    @classmethod
    def _messages(cls, messages: tuple[LlmMessage, ...]) -> list[dict[str, Any]]:
        """将领域消息转换为 Chat Completions 消息序列。"""
        result: list[dict[str, Any]] = []
        for message in messages:
            role = message.role.value
            # 视觉图片只能通过 USER 角色发送，严禁出现在 SYSTEM / ASSISTANT / TOOL 消息中。
            if message.has_image_parts() and role != LlmRole.USER.value:
                raise LlmProtocolError("llm_chat_image_role_invalid")
            if message.tool_calls:
                result.append(
                    {
                        "role": "assistant",
                        "content": _plain_text_content(message) or None,
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
            elif role == LlmRole.TOOL.value:
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": _plain_text_content(message),
                    }
                )
            elif message.content_parts:
                result.append({"role": role, "content": cls._serialize_content_parts(message)})
            else:
                result.append({"role": role, "content": message.content})
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
        """将领域 tool_choice 映射为 Chat Completions 协议值。"""
        if request.tool_choice in {"auto", "none", "required"}:
            return request.tool_choice
        if request.tool_choice.startswith("tool:"):
            name = request.tool_choice[5:]
            if not name:
                raise LlmRequestError("llm_tool_choice_invalid")
            return {"type": "function", "function": {"name": name}}
        raise LlmRequestError("llm_tool_choice_invalid")

    async def _iter_chunks(self, stream: Any) -> AsyncIterator[Any]:
        """安全迭代上游异步流，并将供应商异常映射为固定安全错误码。"""
        iterator = aiter(stream)
        while True:
            try:
                yield await anext(iterator)
            except StopAsyncIteration:
                break
            except asyncio.CancelledError:
                raise
            except LlmError:
                raise
            except Exception as error:
                self._raise_provider_error(error)

    def _tool_fragment_events(
        self,
        calls: dict[int, _ToolCallAccumulator],
        fragment: Any,
    ) -> Iterator[LlmStreamEvent]:
        """累积一个工具调用分片，并按现有顺序产出开始与参数增量事件。"""
        index = fragment.index
        state = calls.get(index)
        if state is None:
            if len(calls) >= MAX_TOOL_CALLS:
                raise LlmProtocolError("llm_stream_too_large")
            state = _ToolCallAccumulator()
            calls[index] = state
        function = fragment.function
        call_id = getattr(fragment, "id", None)
        name = getattr(function, "name", None)
        if call_id:
            if state.call_id and state.call_id != call_id:
                raise LlmProtocolError("llm_tool_call_identity_invalid")
            state.call_id = call_id
        if name:
            if state.name and state.name != name:
                raise LlmProtocolError("llm_tool_call_identity_invalid")
            state.name = name
        if call_id or name:
            yield ToolCallStarted(output_index=index, call_id=state.call_id, name=state.name)
        arguments = getattr(function, "arguments", None)
        if arguments:
            if not isinstance(arguments, str):
                raise LlmProtocolError("llm_tool_arguments_invalid")
            state.argument_bytes += len(arguments.encode("utf-8"))
            if state.argument_bytes > MAX_TOOL_ARGUMENT_BYTES:
                raise LlmProtocolError("llm_tool_arguments_too_large")
            state.arguments += arguments
            yield ToolArgumentsDelta(output_index=index, call_id=state.call_id, delta=arguments)

    @staticmethod
    def _finalize_stream_events(
        *,
        finish_reason: str | None,
        text_parts: list[str],
        calls: dict[int, _ToolCallAccumulator],
        model: str,
        response_id: str | None,
        usage: LlmUsage | None,
    ) -> Iterator[LlmStreamEvent]:
        """校验流结束状态、重组完整工具调用并产出收尾事件。"""
        # 只接受正常停止或工具调用结束；长度截断等其他原因都视为不完整。
        if finish_reason not in {"stop", "tool_calls"}:
            raise LlmIncompleteError("llm_incomplete")
        output: list[LlmText | LlmToolCall] = []
        if text_parts:
            output.append(LlmText("".join(text_parts)))
        if calls and finish_reason != "tool_calls":
            raise LlmProtocolError()
        if finish_reason == "tool_calls" and not calls:
            raise LlmProtocolError("llm_tool_calls_missing")
        # 最终校验：调用 id 唯一、参数是合法 JSON 对象，才生成完整的 LlmToolCall。
        call_ids = [state.call_id for state in calls.values()]
        if len(set(call_ids)) != len(call_ids):
            raise LlmProtocolError("llm_tool_call_identity_invalid")
        for index in sorted(calls):
            state = calls[index]
            try:
                arguments = json.loads(state.arguments)
            except (TypeError, ValueError):
                raise LlmProtocolError("llm_tool_arguments_invalid") from None
            if not state.call_id or not state.name or not isinstance(arguments, dict):
                raise LlmProtocolError("llm_tool_arguments_invalid")
            call = LlmToolCall(state.call_id, state.name, arguments)
            output.append(call)
            yield ToolCallCompleted(output_index=index, tool_call=call)
        # 只有完整重组成功后才发出 ResponseCompleted，执行器据此确认调用成功。
        result = LlmResponse(
            output=tuple(output),
            status="completed",
            incomplete_reason=None,
            model=model,
            response_id=response_id,
            request_id=None,
            usage=usage,
        )
        yield ResponseCompleted(result)

    async def _events(
        self, stream: Any, runtime: RuntimeModelProfile
    ) -> AsyncIterator[LlmStreamEvent]:
        """重组分片调用，等待结束 chunk 后才发出完整结果。"""
        # 应用层聚焦分片计数、文本输出体积与工具调用数量/参数体积上限。
        text_parts: list[str] = []
        calls: dict[int, _ToolCallAccumulator] = {}
        finish_reason: str | None = None
        usage: LlmUsage | None = None
        response_id: str | None = None
        text_bytes = 0
        chunk_count = 0

        async for chunk in self._iter_chunks(stream):
            chunk_count += 1
            if chunk_count > MAX_STREAM_CHUNKS:
                raise LlmProtocolError("llm_stream_too_large")
            response_id = getattr(chunk, "id", None) or response_id
            # 用量通常在最后一个 chunk 返回，允许覆盖为空的前值。
            raw_usage = getattr(chunk, "usage", None)
            if raw_usage is not None:
                usage = self._usage(raw_usage)
            choices = getattr(chunk, "choices", ())
            if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
                raise LlmProtocolError("llm_stream_choice_invalid")
            # 本适配器只请求单候选回答；多 choice 或非零 index 会破坏累积语义，必须整体拒绝。
            if len(choices) > 1:
                raise LlmProtocolError("llm_stream_choice_invalid")
            if not choices:
                continue
            choice = choices[0]
            choice_index = getattr(choice, "index", None)
            if (
                not isinstance(choice_index, int)
                or isinstance(choice_index, bool)
                or choice_index != 0
            ):
                raise LlmProtocolError("llm_stream_choice_invalid")
            if choice.finish_reason is not None:
                if finish_reason is not None and finish_reason != choice.finish_reason:
                    raise LlmProtocolError()
                finish_reason = choice.finish_reason
            delta = choice.delta
            if getattr(delta, "refusal", None):
                raise LlmRefusalError("llm_refusal")
            content = getattr(delta, "content", None)
            if content:
                if not isinstance(content, str):
                    raise LlmProtocolError()
                # 文本累计体积受上限约束，防止超长响应进入持久化。
                text_bytes += len(content.encode("utf-8"))
                if text_bytes > MAX_TEXT_BYTES:
                    raise LlmProtocolError("llm_response_too_large")
                text_parts.append(content)
                yield TextDelta(output_index=0, content_index=0, text=content)
            # 工具调用按 index 累积：id/name 只出现一次，arguments 分片拼接。
            for fragment in getattr(delta, "tool_calls", None) or ():
                for tool_event in self._tool_fragment_events(calls, fragment):
                    yield tool_event

        for final_event in self._finalize_stream_events(
            finish_reason=finish_reason,
            text_parts=text_parts,
            calls=calls,
            model=runtime.version.model,
            response_id=response_id,
            usage=usage,
        ):
            yield final_event

    @staticmethod
    def _raise_provider_error(error: Exception) -> None:
        """映射供应商异常为固定安全错误码。"""
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
