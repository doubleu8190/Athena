"""OpenAI Responses API 适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, NoReturn, cast

import httpx2
import openai
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from openai import AsyncOpenAI
from openai.types.responses import Response

from app.llm.config import LlmProfile
from domain.llm.model import (
    LlmError,
    LlmIncompleteError,
    LlmMessage,
    LlmOutput,
    LlmProtocolError,
    LlmProviderError,
    LlmRefusalError,
    LlmRequest,
    LlmResponse,
    LlmRole,
    LlmStreamEvent,
    LlmText,
    LlmToolArgumentsError,
    LlmToolCall,
    LlmUsage,
    ResponseCompleted,
    TextDelta,
    ToolArgumentsDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from domain.llm.ports import LlmProvider

logger = logging.getLogger(__name__)


class ResponseLimitError(Exception):
    """HTTP response body exceeds configured raw byte limits."""


def _caused_by_response_limit(error: BaseException) -> bool:
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, ResponseLimitError):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class _BoundedResponseStream(httpx2.AsyncByteStream):
    """Enforce raw body, chunk, and SSE event limits while reading transport bytes."""

    def __init__(self, stream: httpx2.AsyncByteStream, limit: int, is_sse: bool) -> None:
        self._stream = stream
        self._limit = limit
        self._is_sse = is_sse
        self._total = 0
        self._event_bytes = 0
        self._line_bytes = 0
        self._pending_cr = False

    def _scan_sse(self, chunk: bytes) -> None:
        for byte in chunk:
            self._event_bytes += 1
            if self._event_bytes > self._limit:
                raise ResponseLimitError("llm_stream_event_too_large")
            if self._pending_cr:
                self._pending_cr = False
                if byte == 0x0A:
                    continue
            if byte == 0x0D:
                self._finish_sse_line()
                self._pending_cr = True
            elif byte == 0x0A:
                self._finish_sse_line()
            else:
                self._line_bytes += 1

    def _finish_sse_line(self) -> None:
        if self._line_bytes == 0:
            self._event_bytes = 0
        self._line_bytes = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._total += len(chunk)
            if self._total > self._limit:
                raise ResponseLimitError("llm_response_too_large")
            if self._is_sse:
                self._scan_sse(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _BoundedResponseTransport(httpx2.AsyncBaseTransport):
    """Wrap HTTP transport responses with per-client byte limits."""

    def __init__(self, inner: httpx2.AsyncBaseTransport, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        content_encoding = response.headers.get("content-encoding", "").strip().lower()
        if content_encoding not in {"", "identity"}:
            try:
                await response.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("llm_response_close_failed code=llm_cleanup_failed")
            raise ResponseLimitError("llm_response_encoding_unsupported") from None
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._limit:
                    await response.aclose()
                    raise ResponseLimitError("llm_response_too_large")
            except ValueError:
                await response.aclose()
                raise ResponseLimitError("llm_response_length_invalid") from None
        is_sse = "text/event-stream" in response.headers.get("content-type", "").lower()
        response.stream = _BoundedResponseStream(
            cast(httpx2.AsyncByteStream, response.stream), self._limit, is_sse
        )
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _plain(value: Any) -> Any:
    """把冻结的领域 JSON 值还原为普通可序列化容器。"""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _dump(value: Any) -> dict[str, Any]:
    """安全地把 SDK 数据转换为领域可序列化字典。"""
    if hasattr(value, "model_dump"):
        return cast(dict[str, Any], value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        return dict(value)
    raise LlmProtocolError()


class OpenAIResponsesAdapter(LlmProvider):
    """按 Profile 复用客户端并实现 Responses 协议转换。"""

    def __init__(self, profiles: Mapping[str, LlmProfile]) -> None:
        """初始化连接池、独立 Profile 配额及客户端复用表。"""
        self._profiles = dict(profiles)
        self._semaphores = {
            name: asyncio.Semaphore(profile.max_concurrency) for name, profile in profiles.items()
        }
        self._clients: dict[tuple[str, str, int], AsyncOpenAI] = {}
        self._closed = False

    async def complete(self, profile: str, request: LlmRequest) -> LlmResponse:
        """调用 Responses 一次并转换完整输出。"""
        selected_profile = self._profile(profile)
        semaphore = self._semaphores[profile]
        timeout = request.timeout_seconds or selected_profile.request_timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                async with semaphore:
                    create = cast(Any, self._client(selected_profile).responses.create)
                    result = await create(
                        model=selected_profile.model,
                        input=self._input(request.messages),
                        tools=self._tools(request),
                        tool_choice=self._tool_choice(request),
                        max_output_tokens=request.max_output_tokens
                        or selected_profile.max_output_tokens,
                        include=["reasoning.encrypted_content"],
                        store=False,
                        **self._temperature(request),
                    )
            return self._response(result, request, selected_profile)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._raise_provider_error(error)

    def stream(
        self, profile: str, request: LlmRequest
    ) -> AbstractAsyncContextManager[AsyncIterator[LlmStreamEvent]]:
        """建立具备按需消费和提前关闭能力的 Responses 流。"""
        return self._stream(profile, request)

    @asynccontextmanager
    async def _stream(
        self, profile_name: str, request: LlmRequest
    ) -> AsyncIterator[AsyncIterator[LlmStreamEvent]]:
        """在异步上下文全程持有并发配额及上游连接。"""
        profile = self._profile(profile_name)
        semaphore = self._semaphores[profile_name]
        stream: Any = None
        timeout_seconds = request.timeout_seconds or profile.stream_timeout_seconds
        acquired = False
        try:
            async with asyncio.timeout(timeout_seconds):
                await semaphore.acquire()
                acquired = True
                try:
                    create = cast(Any, self._client(profile).responses.create)
                    stream = await create(
                        model=profile.model,
                        input=self._input(request.messages),
                        tools=self._tools(request),
                        tool_choice=self._tool_choice(request),
                        max_output_tokens=request.max_output_tokens or profile.max_output_tokens,
                        include=["reasoning.encrypted_content"],
                        store=False,
                        stream=True,
                        **self._temperature(request),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._raise_provider_error(error)
                yield self._events(stream, request, profile)
        except TimeoutError:
            raise LlmProviderError("llm_timeout") from None
        finally:
            try:
                if stream is not None:
                    try:
                        closer = getattr(stream, "close", None) or getattr(stream, "aclose", None)
                        if closer is not None:
                            await closer()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("llm_stream_close_failed code=llm_cleanup_failed")
            finally:
                if acquired:
                    semaphore.release()

    async def generate_structured(
        self,
        profile: str,
        request: LlmRequest,
        schema_name: str,
        schema: Mapping[str, Any],
    ) -> LlmResponse:
        """调用一次 Responses strict JSON Schema 输出。"""
        profile_config = self._profile(profile)
        semaphore = self._semaphores[profile]
        timeout = request.timeout_seconds or profile_config.request_timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                async with semaphore:
                    create = cast(Any, self._client(profile_config).responses.create)
                    result = await create(
                        model=profile_config.model,
                        input=self._input(request.messages),
                        text={
                            "format": {
                                "type": "json_schema",
                                "name": schema_name,
                                "strict": True,
                                "schema": dict(schema),
                            }
                        },
                        max_output_tokens=request.max_output_tokens
                        or profile_config.max_output_tokens,
                        store=False,
                        **self._temperature(request),
                    )
            return self._response(result, request, profile_config)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._raise_provider_error(error)

    async def aclose(self) -> None:
        """逐一关闭客户端；单个关闭失败不影响其他连接池。"""
        if self._closed:
            return
        self._closed = True
        errors = 0
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:
                errors += 1
                logger.warning("llm_client_close_failed code=llm_cleanup_failed")
        self._clients.clear()
        if errors:
            logger.warning("llm_client_close_summary failures=%d", errors)

    def _client(self, profile: LlmProfile) -> AsyncOpenAI:
        identity = hashlib.sha256(profile.api_key.encode()).hexdigest()
        key = (
            profile.base_url or "https://api.openai.com/v1",
            identity,
            profile.max_response_bytes,
        )
        client = self._clients.get(key)
        if client is None:
            http_client = httpx2.AsyncClient(
                headers={"accept-encoding": "identity"},
                transport=_BoundedResponseTransport(
                    httpx2.AsyncHTTPTransport(), profile.max_response_bytes
                ),
            )
            kwargs: dict[str, Any] = {
                "api_key": profile.api_key,
                "max_retries": 0,
                "http_client": http_client,
            }
            if profile.base_url:
                kwargs["base_url"] = profile.base_url
            client = AsyncOpenAI(**kwargs)
            self._clients[key] = client
        return client

    def _profile(self, name: str) -> LlmProfile:
        try:
            return self._profiles[name]
        except KeyError:
            from domain.llm.model import LlmRequestError

            raise LlmRequestError("llm_profile_unknown") from None

    @staticmethod
    def _input(messages: tuple[LlmMessage, ...]) -> list[dict[str, Any]]:
        """按领域顺序构造 Responses 输入，并保留续接项。"""
        items: list[dict[str, Any]] = []
        for message in messages:
            items.extend(_plain(item) for item in message.continuation_items)
            if message.continuation_items and not message.content:
                continue
            if message.role is LlmRole.TOOL:
                if not message.tool_call_id:
                    raise LlmProtocolError()
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content,
                    }
                )
            else:
                items.append({"role": message.role.value, "content": message.content})
        return items

    @staticmethod
    def _tools(request: LlmRequest) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": _plain(tool.input_schema),
                "strict": False,
            }
            for tool in request.tools
        ]

    @staticmethod
    def _tool_choice(request: LlmRequest) -> str | dict[str, str]:
        if request.tool_choice.startswith("tool:"):
            return {"type": "function", "name": request.tool_choice[5:]}
        return request.tool_choice

    @staticmethod
    def _temperature(request: LlmRequest) -> dict[str, float]:
        return {"temperature": request.temperature} if request.temperature is not None else {}

    def _response(self, result: Response, request: LlmRequest, profile: LlmProfile) -> LlmResponse:
        """将 completed 响应转换成有序领域输出并验证工具参数。"""
        if result.status == "incomplete":
            raise LlmIncompleteError()
        if result.status == "failed":
            raise LlmProviderError("llm_response_failed")
        if result.status != "completed":
            raise LlmProtocolError()
        output: list[LlmOutput] = []
        continuation = tuple(_dump(item) for item in result.output)
        try:
            output_bytes = len(json.dumps(continuation, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            raise LlmProtocolError() from None
        if output_bytes > profile.max_response_bytes:
            raise LlmProtocolError("llm_response_too_large")
        schemas = {tool.name: tool.input_schema for tool in request.tools}
        call_ids: set[str] = set()
        try:
            for item in result.output:
                if item.type == "reasoning":
                    self._validate_reasoning(item)
                    continue
                if item.type == "function_call":
                    if (
                        not item.call_id
                        or item.call_id in call_ids
                        or not item.name
                        or item.name not in schemas
                    ):
                        raise LlmProtocolError()
                    call_ids.add(item.call_id)
                    if len(item.arguments.encode("utf-8")) > profile.max_tool_argument_bytes:
                        raise LlmProtocolError("llm_tool_arguments_too_large")
                    if (
                        sum(isinstance(output_item, LlmToolCall) for output_item in output)
                        >= profile.max_tools
                    ):
                        raise LlmProtocolError("llm_tool_count_exceeded")
                    arguments = json.loads(item.arguments)
                    if not isinstance(arguments, dict):
                        raise LlmProtocolError()
                    Draft202012Validator(_plain(schemas[item.name])).validate(arguments)
                    output.append(LlmToolCall(item.call_id, item.name, arguments))
                elif item.type == "message":
                    if item.role != "assistant":
                        raise LlmProtocolError("llm_message_role_invalid")
                    for content in item.content:
                        if content.type == "refusal":
                            raise LlmRefusalError()
                        if content.type == "output_text":
                            output.append(LlmText(content.text))
                        else:
                            raise LlmProtocolError("llm_content_type_unsupported")
                else:
                    raise LlmProtocolError("llm_output_type_unsupported")
        except LlmRefusalError:
            raise
        except LlmProtocolError:
            raise
        except Exception:
            raise LlmToolArgumentsError() from None
        raw_usage = result.usage
        usage = (
            None
            if raw_usage is None
            else LlmUsage(raw_usage.input_tokens, raw_usage.output_tokens, raw_usage.total_tokens)
        )
        return LlmResponse(
            tuple(output),
            result.status,
            result.incomplete_details.reason if result.incomplete_details else None,
            result.model,
            result.id,
            getattr(result, "_request_id", None),
            usage,
            continuation,
        )

    @staticmethod
    def _validate_reasoning(item: Any) -> None:
        """拒绝无法完整安全重放的 Responses reasoning 项。"""
        raw_item = _dump(item)
        if not isinstance(raw_item.get("id"), str) or not raw_item["id"]:
            raise LlmProtocolError("llm_reasoning_item_invalid")
        if not isinstance(raw_item.get("encrypted_content", ""), str):
            raise LlmProtocolError("llm_reasoning_item_invalid")
        for field_name, expected_type in (
            ("summary", "summary_text"),
            ("content", "reasoning_text"),
        ):
            entries = raw_item.get(field_name, [])
            if not isinstance(entries, list):
                raise LlmProtocolError("llm_reasoning_item_invalid")
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or entry.get("type") != expected_type
                    or not isinstance(entry.get("text"), str)
                ):
                    raise LlmProtocolError("llm_reasoning_item_invalid")

    async def _events(
        self, stream: Any, request: LlmRequest, profile: LlmProfile
    ) -> AsyncIterator[LlmStreamEvent]:
        """按 output 和 content 索引重组事件并逐项核对终态响应。"""
        states: dict[int, dict[str, Any]] = {}
        call_ids: set[str] = set()
        accumulated_bytes = 0
        final_response: Response | None = None
        terminal_seen = False
        completed_tools: list[tuple[int, LlmToolCall]] = []

        def item_state(index: int, item_id: str, item_type: str) -> dict[str, Any]:
            state = states.get(index)
            if (
                state is None
                or state["id"] != item_id
                or state["type"] != item_type
                or state["done"]
            ):
                raise LlmProtocolError("llm_stream_item_mismatch")
            return state

        try:
            async for event in stream:
                if terminal_seen:
                    raise LlmProtocolError("llm_multiple_terminal_events")
                kind = event.type
                if kind == "response.output_item.added":
                    if event.output_index != len(states):
                        raise LlmProtocolError("llm_output_order_invalid")
                    raw_item = _dump(event.item)
                    item_type = raw_item.get("type")
                    item_id = raw_item.get("id")
                    if item_type not in {"message", "reasoning", "function_call"}:
                        raise LlmProtocolError("llm_output_type_unsupported")
                    if not isinstance(item_id, str) or not item_id:
                        raise LlmProtocolError("llm_output_item_id_missing")
                    state: dict[str, Any] = {
                        "id": item_id,
                        "type": item_type,
                        "done": False,
                        "added": raw_item,
                        "contents": {},
                        "reasoning_text": {},
                        "reasoning_summary": {},
                    }
                    if item_type == "message":
                        if raw_item.get("role") != "assistant":
                            raise LlmProtocolError("llm_message_role_invalid")
                        state["content_done"] = set()
                    elif item_type == "reasoning":
                        for key, target, expected in (
                            ("content", "reasoning_text", "reasoning_text"),
                            ("summary", "reasoning_summary", "summary_text"),
                        ):
                            entries = raw_item.get(key, [])
                            if not isinstance(entries, list):
                                raise LlmProtocolError("llm_reasoning_item_invalid")
                            for part_index, part in enumerate(entries):
                                if (
                                    not isinstance(part, dict)
                                    or part.get("type") != expected
                                    or not isinstance(part.get("text", ""), str)
                                ):
                                    raise LlmProtocolError("llm_reasoning_item_invalid")
                                state[target][part_index] = {
                                    "text": part.get("text", ""),
                                    "text_done": False,
                                    "part_done": False,
                                }
                    else:
                        call_id = raw_item.get("call_id")
                        name = raw_item.get("name")
                        if (
                            not isinstance(call_id, str)
                            or not call_id
                            or call_id in call_ids
                            or not isinstance(name, str)
                            or not name
                            or len(call_ids) >= profile.max_tools
                        ):
                            raise LlmProtocolError("llm_function_call_identity_invalid")
                        call_ids.add(call_id)
                        state.update(
                            call_id=call_id,
                            name=name,
                            arguments="",
                            arguments_done=False,
                        )
                        yield ToolCallStarted(event.output_index, call_id, name)
                    states[event.output_index] = state
                    accumulated_bytes += len(json.dumps(raw_item, ensure_ascii=False).encode())
                elif kind == "response.content_part.added":
                    state = item_state(event.output_index, event.item_id, "message")
                    content_index = event.content_index
                    if content_index != len(state["contents"]):
                        raise LlmProtocolError("llm_content_order_invalid")
                    raw_part = _dump(event.part)
                    part_type = raw_part.get("type")
                    if part_type == "refusal":
                        raise LlmRefusalError()
                    if part_type != "output_text" or not isinstance(raw_part.get("text", ""), str):
                        raise LlmProtocolError("llm_content_type_unsupported")
                    state["contents"][content_index] = {
                        "type": part_type,
                        "text": raw_part.get("text", ""),
                        "text_done": False,
                    }
                    accumulated_bytes += len(raw_part.get("text", "").encode())
                elif kind == "response.output_text.delta":
                    state = item_state(event.output_index, event.item_id, "message")
                    content = state["contents"].get(event.content_index)
                    if content is None or content["text_done"]:
                        raise LlmProtocolError("llm_content_delta_invalid")
                    content["text"] += event.delta
                    accumulated_bytes += len(event.delta.encode("utf-8"))
                    if accumulated_bytes > profile.max_response_bytes:
                        raise LlmProtocolError("llm_response_too_large")
                    yield TextDelta(event.output_index, event.content_index, event.delta)
                elif kind == "response.output_text.done":
                    state = item_state(event.output_index, event.item_id, "message")
                    content = state["contents"].get(event.content_index)
                    if content is None or content["text_done"] or content["text"] != event.text:
                        raise LlmProtocolError("llm_output_text_done_mismatch")
                    content["text_done"] = True
                elif kind == "response.content_part.done":
                    state = item_state(event.output_index, event.item_id, "message")
                    content = state["contents"].get(event.content_index)
                    raw_part = _dump(event.part)
                    if (
                        content is None
                        or event.content_index in state["content_done"]
                        or not content["text_done"]
                        or raw_part.get("type") != "output_text"
                        or raw_part.get("text") != content["text"]
                    ):
                        if raw_part.get("type") == "refusal":
                            raise LlmRefusalError()
                        raise LlmProtocolError("llm_content_part_done_mismatch")
                    state["content_done"].add(event.content_index)
                elif kind == "response.function_call_arguments.delta":
                    state = item_state(event.output_index, event.item_id, "function_call")
                    if state["arguments_done"]:
                        raise LlmProtocolError("llm_tool_arguments_after_done")
                    state["arguments"] += event.delta
                    arg_size = len(state["arguments"].encode("utf-8"))
                    accumulated_bytes += len(event.delta.encode("utf-8"))
                    if arg_size > profile.max_tool_argument_bytes:
                        raise LlmProtocolError("llm_tool_arguments_too_large")
                    if accumulated_bytes > profile.max_response_bytes:
                        raise LlmProtocolError("llm_response_too_large")
                    yield ToolArgumentsDelta(event.output_index, state["call_id"], event.delta)
                elif kind == "response.function_call_arguments.done":
                    state = item_state(event.output_index, event.item_id, "function_call")
                    if state["arguments_done"] or state["arguments"] != event.arguments:
                        raise LlmProtocolError("llm_tool_arguments_done_mismatch")
                    schemas = {tool.name: tool.input_schema for tool in request.tools}
                    if state["name"] not in schemas:
                        raise LlmProtocolError("llm_tool_name_unbound")
                    try:
                        arguments = json.loads(event.arguments)
                        if not isinstance(arguments, dict):
                            raise ValueError
                        Draft202012Validator(_plain(schemas[state["name"]])).validate(arguments)
                    except Exception:
                        raise LlmToolArgumentsError() from None
                    state["arguments_done"] = True
                    state["arguments_value"] = arguments
                elif kind in {
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                }:
                    state = item_state(event.output_index, event.item_id, "reasoning")
                    target = (
                        "reasoning_text"
                        if kind == "response.reasoning_text.delta"
                        else "reasoning_summary"
                    )
                    part_index = (
                        event.content_index if target == "reasoning_text" else event.summary_index
                    )
                    part = state[target].setdefault(
                        part_index, {"text": "", "text_done": False, "part_done": False}
                    )
                    if part["text_done"]:
                        raise LlmProtocolError("llm_reasoning_delta_after_done")
                    part["text"] += event.delta
                    accumulated_bytes += len(event.delta.encode("utf-8"))
                    if accumulated_bytes > profile.max_response_bytes:
                        raise LlmProtocolError("llm_response_too_large")
                elif kind in {
                    "response.reasoning_text.done",
                    "response.reasoning_summary_text.done",
                }:
                    state = item_state(event.output_index, event.item_id, "reasoning")
                    target = (
                        "reasoning_text"
                        if kind == "response.reasoning_text.done"
                        else "reasoning_summary"
                    )
                    part_index = (
                        event.content_index if target == "reasoning_text" else event.summary_index
                    )
                    part = state[target].get(part_index)
                    if part is None or part["text_done"] or part["text"] != event.text:
                        raise LlmProtocolError("llm_reasoning_done_mismatch")
                    part["text_done"] = True
                elif kind == "response.reasoning_summary_part.added":
                    state = item_state(event.output_index, event.item_id, "reasoning")
                    part = _dump(event.part)
                    index = event.summary_index
                    if (
                        index != len(state["reasoning_summary"])
                        or part.get("type") != "summary_text"
                        or not isinstance(part.get("text", ""), str)
                    ):
                        raise LlmProtocolError("llm_reasoning_summary_invalid")
                    state["reasoning_summary"][index] = {
                        "text": part.get("text", ""),
                        "text_done": False,
                        "part_done": False,
                    }
                elif kind == "response.reasoning_summary_part.done":
                    state = item_state(event.output_index, event.item_id, "reasoning")
                    part = _dump(event.part)
                    current = state["reasoning_summary"].get(event.summary_index)
                    if (
                        current is None
                        or current["part_done"]
                        or current["text"] != part.get("text")
                        or part.get("type") != "summary_text"
                    ):
                        raise LlmProtocolError("llm_reasoning_summary_done_mismatch")
                    current["part_done"] = True
                elif kind == "response.output_item.done":
                    state = item_state(event.output_index, event.item.id, event.item.type)
                    raw_done = _dump(event.item)
                    if state["type"] == "message":
                        final_content = raw_done.get("content")
                        if (
                            not isinstance(final_content, list)
                            or len(final_content) != len(state["contents"])
                            or set(state["contents"]) != state["content_done"]
                            or any(
                                not content["text_done"] for content in state["contents"].values()
                            )
                            or any(
                                not isinstance(part, dict)
                                or part.get("type") != state["contents"][index]["type"]
                                or part.get("text") != state["contents"][index]["text"]
                                for index, part in enumerate(final_content)
                            )
                        ):
                            raise LlmProtocolError("llm_content_done_missing")
                    elif state["type"] == "function_call":
                        if (
                            not state["arguments_done"]
                            or raw_done.get("call_id") != state["call_id"]
                            or raw_done.get("name") != state["name"]
                            or raw_done.get("arguments") != state["arguments"]
                        ):
                            raise LlmProtocolError("llm_tool_item_done_mismatch")
                        completed_tools.append(
                            (
                                event.output_index,
                                LlmToolCall(
                                    state["call_id"],
                                    state["name"],
                                    state["arguments_value"],
                                ),
                            )
                        )
                    else:
                        if any(
                            not part["text_done"] for part in state["reasoning_text"].values()
                        ) or any(
                            not part["part_done"] for part in state["reasoning_summary"].values()
                        ):
                            raise LlmProtocolError("llm_reasoning_done_missing")
                        for key, target, expected in (
                            ("content", "reasoning_text", "reasoning_text"),
                            ("summary", "reasoning_summary", "summary_text"),
                        ):
                            final_parts = raw_done.get(key, [])
                            accumulated = state[target]
                            if (
                                not isinstance(final_parts, list)
                                or len(final_parts) != len(accumulated)
                                or any(
                                    not isinstance(part, dict)
                                    or part.get("type") != expected
                                    or part.get("text") != accumulated[index]["text"]
                                    for index, part in enumerate(final_parts)
                                )
                            ):
                                raise LlmProtocolError("llm_reasoning_item_mismatch")
                    state["done"] = True
                    state["done_item"] = raw_done
                elif kind == "response.completed":
                    terminal_seen = True
                    final_response = event.response
                elif kind == "response.incomplete":
                    raise LlmIncompleteError()
                elif kind == "response.failed":
                    raise LlmProviderError("llm_response_failed")
                elif kind in {"response.refusal.delta", "response.refusal.done"}:
                    raise LlmRefusalError()
                elif kind == "error":
                    raise LlmProviderError()
                elif kind in {
                    "response.created",
                    "response.queued",
                    "response.in_progress",
                }:
                    continue
                elif kind == "response.output_text.annotation.added":
                    state = item_state(event.output_index, event.item_id, "message")
                    if event.content_index not in state["contents"]:
                        raise LlmProtocolError("llm_annotation_content_missing")
                else:
                    raise LlmProtocolError("llm_event_unknown")
            if final_response is None or not terminal_seen:
                raise LlmProtocolError("llm_terminal_event_missing")
            if not all(state["done"] for state in states.values()):
                raise LlmProtocolError("llm_output_item_done_missing")
            if len(final_response.output) != len(states):
                raise LlmProtocolError("llm_output_count_mismatch")
            for index, final_item in enumerate(final_response.output):
                final_state = states.get(index)
                raw_final = _dump(final_item)
                if (
                    final_state is None
                    or final_state["done_item"] != raw_final
                    or final_state["id"] != raw_final.get("id")
                    or final_state["type"] != raw_final.get("type")
                ):
                    raise LlmProtocolError("llm_output_item_mismatch")
            response = self._response(final_response, request, profile)
            for output_index, tool_call in sorted(completed_tools, key=lambda item: item[0]):
                yield ToolCallCompleted(output_index, tool_call)
            yield ResponseCompleted(response)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._raise_provider_error(error)

    @staticmethod
    def _raise_provider_error(error: Exception) -> NoReturn:
        """把 SDK 异常安全映射为固定领域错误码。"""
        if isinstance(error, LlmError):
            raise error
        if isinstance(error, ResponseLimitError):
            raise LlmProtocolError(str(error)) from None
        if isinstance(error, openai.APIConnectionError) and _caused_by_response_limit(error):
            raise LlmProtocolError("llm_response_encoding_unsupported") from None
        if isinstance(error, openai.AuthenticationError):
            raise LlmProviderError("llm_authentication_failed") from None
        if isinstance(error, openai.RateLimitError):
            raise LlmProviderError("llm_rate_limited") from None
        if isinstance(error, openai.APITimeoutError):
            raise LlmProviderError("llm_timeout") from None
        if isinstance(error, TimeoutError):
            raise LlmProviderError("llm_timeout") from None
        if isinstance(error, openai.APIConnectionError):
            raise LlmProviderError("llm_connection_failed") from None
        raise LlmProviderError() from None
