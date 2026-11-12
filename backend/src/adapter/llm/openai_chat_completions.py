"""OpenAI 兼容 Chat Completions 流式协议适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast

import httpx2
import openai
from openai import AsyncOpenAI

from adapter.llm.bounded_http import (
    BoundedResponseTransport,
    ResponseLimitError,
    caused_by_response_limit,
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
    ModelProfileVersion,
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
MAX_DIAGNOSTIC_FRAGMENTS = 16
_DIAGNOSTIC_DIGEST_LENGTH = 12
_SAFE_FINISH_REASONS = frozenset(
    {"stop", "tool_calls", "length", "content_filter", "function_call"}
)


@dataclass(slots=True)
class _ToolCallAccumulator:
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    argument_bytes: int = 0


def _diagnostic_digest(value: str | None) -> str | None:
    """返回不暴露原值的短 SHA-256 摘要；空值保持为空。"""
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_DIAGNOSTIC_DIGEST_LENGTH]


class _ToolCallDiagnostics:
    """收集单次请求内有界、脱敏的工具 index/id 分片摘要。"""

    def __init__(self) -> None:
        """初始化请求级计数、最多八个 index 的 id 摘要和少量位置样本。"""
        self.chunk_count = 0
        self.fragment_count = 0
        self.dropped_positions = 0
        self._index_to_id: dict[int, str | None] = {}
        self.duplicate_id_count = 0
        self.id_change_count = 0
        self.positions: list[dict[str, object]] = []
        self.finish_reasons: set[str] = set()
        self.outcome = "incomplete"
        self.error_code: str | None = None
        self._emitted = False

    def observe_chunk(self) -> None:
        """在任何解析前记录本次请求已尝试读取的分片序号。"""
        self.chunk_count += 1

    def observe_finish_reason(self, value: Any) -> None:
        """记录归一化的 choice 完成原因。"""
        self.finish_reasons.add(
            value if isinstance(value, str) and value in _SAFE_FINISH_REASONS else "other"
        )

    def observe_fragment(
        self,
        *,
        choice_index: Any,
        fragment_position: int,
        fragment: Any,
    ) -> None:
        """旁路记录工具 index/id 关系，不复用 parser 的调用内容状态。"""
        self.fragment_count += 1
        raw_index = getattr(fragment, "index", None)
        index = (
            raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else None
        )
        raw_choice_index = choice_index if isinstance(choice_index, int) else None
        function = getattr(fragment, "function", None)
        call_id = getattr(fragment, "id", None)
        name = getattr(function, "name", None)
        arguments = getattr(function, "arguments", None)
        id_digest = _diagnostic_digest(call_id if isinstance(call_id, str) else None)
        name_digest = _diagnostic_digest(name if isinstance(name, str) else None)
        argument_bytes = len(arguments.encode("utf-8")) if isinstance(arguments, str) else 0

        relation = "index_invalid"
        if index is not None:
            existing = index in self._index_to_id
            if existing or len(self._index_to_id) < MAX_TOOL_CALLS:
                relation = (
                    "same_index_continuation"
                    if existing
                    else "independent_call"
                    if self._index_to_id
                    else "new_index"
                )
                previous_id_digest = self._index_to_id.get(index)
                if id_digest is not None:
                    duplicate_id = any(
                        other_index != index and other_id_digest == id_digest
                        for other_index, other_id_digest in self._index_to_id.items()
                    )
                    if duplicate_id:
                        relation = "duplicate_id_across_index"
                        self.duplicate_id_count += 1
                    elif previous_id_digest is not None and previous_id_digest != id_digest:
                        relation = "same_index_id_changed"
                        self.id_change_count += 1
                    self._index_to_id[index] = id_digest
                elif not existing:
                    self._index_to_id[index] = None
            else:
                relation = "index_limit"

        if len(self.positions) < MAX_DIAGNOSTIC_FRAGMENTS:
            self.positions.append(
                {
                    "chunk_sequence": self.chunk_count,
                    "choice_index": raw_choice_index,
                    "fragment_position": fragment_position,
                    "call_index": index,
                    "relation": relation,
                    "has_id": id_digest is not None,
                    "id_sha256": id_digest,
                    "has_name": name_digest is not None,
                    "name_sha256": name_digest,
                    "argument_bytes": argument_bytes,
                }
            )
        else:
            self.dropped_positions += 1

    def emit(self) -> None:
        """在请求结束时最多写一条不含正文、身份原值或连接配置的日志。"""
        if self._emitted or self.fragment_count == 0:
            return
        self._emitted = True
        payload = {
            "event": "llm_chat_stream_diagnostic",
            "protocol": "chat_completions",
            "outcome": self.outcome,
            "error_code": self.error_code,
            "chunks_observed": self.chunk_count,
            "finish_reasons": sorted(self.finish_reasons),
            "index_count": len(self._index_to_id),
            "totals": {
                "fragment_count": self.fragment_count,
                "duplicate_id_count": self.duplicate_id_count,
                "id_change_count": self.id_change_count,
            },
            "calls": [
                {"call_index": index, "id_sha256": id_digest}
                for index, id_digest in sorted(self._index_to_id.items())
            ],
            "positions": self.positions,
            "truncated": self.dropped_positions > 0,
            "dropped_positions": self.dropped_positions,
        }
        logger.info(
            "llm_chat_stream_diagnostic %s",
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        )


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
        semaphore = self._semaphore_for(config)
        timeout = self._request_timeout(config, request)
        request_parameters = self._complete_request_parameters(
            config,
            messages,
            request,
            timeout=timeout,
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
    async def _stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
        """持有并发名额直到流结束、失败或调用方退出。"""
        config = runtime.version
        # 先确认绑定版本支持流式与工具，再申请该版本的并发名额。
        self._validate_capabilities(runtime)
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
                    create = cast(Any, self._client(runtime).chat.completions.create)
                    try:
                        # 只发起一次流式请求（SDK 重试已禁用），重试策略由执行器负责。
                        request_parameters = self._stream_request_parameters(
                            config,
                            request,
                            timeout=timeout,
                        )
                        stream = await create(**request_parameters)
                    except asyncio.CancelledError:
                        raise
                    except LlmError:
                        raise
                    except Exception as error:
                        self._raise_provider_error(error)
                    # 事件转换器负责分片重组、体积上限与协议校验。
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
    def _validate_capabilities(runtime: RuntimeModelProfile) -> None:
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
                transport=BoundedResponseTransport(
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
            # 显式覆盖 SDK 默认 600 秒，使 HTTP 超时与本次 Run/Profile
            # deadline 一致；客户端仍可按 base_url 和密钥安全复用。
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
        """构造无工具、非流式的完整响应请求参数。"""
        return {
            **cls._common_request_parameters(
                config,
                messages,
                request,
                timeout=timeout,
            ),
            "tools": [],
            "tool_choice": "none",
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
        return {
            **self._common_request_parameters(
                config,
                self._messages(request.messages),
                request,
                timeout=timeout,
            ),
            "tools": self._tools(request),
            "tool_choice": self._tool_choice(request),
            "parallel_tool_calls": config.supports_parallel_tool_calls,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

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
        """校验非流式响应并转换为唯一文本结果。"""
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
        return LlmResponse(
            output=(LlmText(content),),
            status="completed",
            incomplete_reason=None,
            model=getattr(response, "model", None) or config.model,
            response_id=getattr(response, "id", None),
            request_id=None,
            usage=self._usage(getattr(response, "usage", None)),
        )

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

    @staticmethod
    def _measure_stream_chunk(
        chunk: Any,
        chunk_count: int,
        total_raw_bytes: int,
    ) -> tuple[int, int]:
        """执行分片数量、单分片和累计原始字节检查。"""
        chunk_count += 1
        if chunk_count > MAX_STREAM_CHUNKS:
            raise LlmProtocolError("llm_stream_too_large")
        try:
            chunk_bytes = len(
                json.dumps(_json_value(chunk), ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeError):
            raise LlmProtocolError("llm_stream_event_invalid") from None
        if chunk_bytes > MAX_RAW_EVENT_BYTES:
            raise LlmProtocolError("llm_stream_event_too_large")
        total_raw_bytes += chunk_bytes
        if total_raw_bytes > MAX_TOTAL_RAW_BYTES:
            raise LlmProtocolError("llm_stream_too_large")
        return chunk_count, total_raw_bytes

    async def _tool_fragment_events(
        self,
        calls: dict[int, _ToolCallAccumulator],
        fragment: Any,
    ) -> AsyncIterator[LlmStreamEvent]:
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

    async def _events(
        self, stream: Any, runtime: RuntimeModelProfile
    ) -> AsyncIterator[LlmStreamEvent]:
        """重组分片调用，等待结束 chunk 后才发出完整结果。"""
        # 分片文本与工具调用参数先累积，只有完整结束后才产出最终 LlmResponse。
        text_parts: list[str] = []
        calls: dict[int, _ToolCallAccumulator] = {}
        finish_reason: str | None = None
        usage: LlmUsage | None = None
        response_id: str | None = None
        total_raw_bytes = 0
        text_bytes = 0
        chunk_count = 0
        diagnostics = _ToolCallDiagnostics()
        iterator = aiter(stream)
        try:
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
                diagnostics.observe_chunk()
                # 对分片数量、单片体积与累计体积设上限，防止异常流耗尽资源。
                chunk_count, total_raw_bytes = self._measure_stream_chunk(
                    chunk, chunk_count, total_raw_bytes
                )
                response_id = getattr(chunk, "id", None) or response_id
                # 用量通常在最后一个 chunk 返回，允许覆盖为空的前值。
                raw_usage = getattr(chunk, "usage", None)
                if raw_usage is not None:
                    usage = self._usage(raw_usage)
                choices = getattr(chunk, "choices", ())
                if not isinstance(choices, Sequence) or isinstance(
                    choices, (str, bytes, bytearray)
                ):
                    raise LlmProtocolError("llm_stream_choice_invalid")
                # 本适配器只请求单候选回答；多 choice 或非零 index 会破坏
                # 文本/工具累积位置和最终响应语义，必须整体拒绝。
                if len(choices) > 1:
                    raise LlmProtocolError("llm_stream_choice_invalid")
                if choices:
                    choice = choices[0]
                    choice_index = getattr(choice, "index", None)
                    if (
                        not isinstance(choice_index, int)
                        or isinstance(choice_index, bool)
                        or choice_index != 0
                    ):
                        raise LlmProtocolError("llm_stream_choice_invalid")
                for choice in choices:
                    if choice.finish_reason is not None:
                        diagnostics.observe_finish_reason(choice.finish_reason)
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
                        # 文本累计体积受上限约束，防止超长响应进入持久化。
                        text_bytes += len(content.encode("utf-8"))
                        if text_bytes > MAX_TEXT_BYTES:
                            raise LlmProtocolError("llm_response_too_large")
                        text_parts.append(content)
                        yield TextDelta(output_index=0, content_index=0, text=content)
                    # 工具诊断只旁路观察原始分片，不参与重组合法性判定。
                    tool_fragments = getattr(delta, "tool_calls", None) or ()
                    for position, fragment in enumerate(tool_fragments):
                        diagnostics.observe_fragment(
                            choice_index=getattr(choice, "index", None),
                            fragment_position=position,
                            fragment=fragment,
                        )
                        # 工具调用按 index 累积：id/name 只出现一次，arguments 分片拼接。
                        async for tool_event in self._tool_fragment_events(calls, fragment):
                            yield tool_event
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
                model=runtime.version.model,
                response_id=response_id,
                request_id=None,
                usage=usage,
            )
            diagnostics.outcome = "completed"
            yield ResponseCompleted(result)
        except asyncio.CancelledError:
            diagnostics.outcome = "cancelled"
            raise
        except LlmError as error:
            code = error.code
            diagnostics.outcome = "rejected"
            diagnostics.error_code = (
                code
                if code.isascii()
                and 1 <= len(code) <= 80
                and all(
                    character.islower() or character.isdigit() or character == "_"
                    for character in code
                )
                else "llm_error"
            )
            raise
        except Exception:
            diagnostics.outcome = "rejected"
            diagnostics.error_code = "llm_provider_error"
            raise
        finally:
            diagnostics.emit()

    @staticmethod
    def _raise_provider_error(error: Exception) -> None:
        """映射供应商异常为固定安全错误码。"""
        if isinstance(error, ResponseLimitError):
            raise LlmProtocolError(str(error)) from None
        if isinstance(error, openai.APIConnectionError) and caused_by_response_limit(error):
            current: BaseException | None = error
            visited: set[int] = set()
            while current is not None and id(current) not in visited:
                if isinstance(current, ResponseLimitError):
                    raise LlmProtocolError(str(current)) from None
                visited.add(id(current))
                current = current.__cause__ or current.__context__
            raise LlmProtocolError("llm_provider_error") from None
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
