"""Run 请求中的固定长期记忆与历史检索结果复核。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

import tiktoken
from pydantic import BaseModel, ConfigDict, Field

from app.memory.context import MemoryContextService
from domain.memory.lifecycle import (
    MemoryContextItem,
    MemoryReferenceKey,
)
from domain.memory.model import AccessContext
from domain.memory.ports import MemoryLifecycleRepositoryPort
from domain.message import (
    ConversationMessage,
    ExecutionMessage,
    ExecutionRole,
    MessagePart,
    ToolCallPart,
    ToolResultPart,
)
from domain.tool import ToolOutcome, ToolOutcomeStatus, protocol_status_for_outcome

_INVALIDATED_MEMORY_TEXT = "[记忆已失效或版本已变化，正文不再提供]"
type RunContextMessage = ConversationMessage | ExecutionMessage


class _MemorySearchItem(BaseModel):
    """memory_search 输出中的服务端引用与当前正文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    content: str
    scope: Literal["personal", "project"]
    invalidated: bool = False


class _MemorySearchData(BaseModel):
    """memory_search 成功结果的严格内部协议。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "degraded"]
    items: tuple[_MemorySearchItem, ...] = ()
    truncated: bool = False
    degradation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedRunMemoryContext:
    """发送前已复核的 always 记忆包装与历史工具消息投影。"""

    content: str
    messages: tuple[RunContextMessage, ...]
    items: tuple[MemoryContextItem, ...]
    contains_memory: bool


class RuntimeTokenCounter:
    """使用当前 Run 固定模型版本的 tokenizer 计算记忆预算。"""

    def __init__(self, encoding_name: str) -> None:
        """加载不可变的 tokenizer 编码名称。"""
        try:
            self._encoding = tiktoken.get_encoding(encoding_name)
        except (KeyError, ValueError) as error:
            raise ValueError("run_tokenizer_unsupported") from error

    def count(self, text: str) -> int:
        """返回与 Run 总窗口测量一致的普通文本 token 数。"""
        return len(self._encoding.encode_ordinary(text))


class RunMemoryContextManager:
    """装配一次固定记忆集合，并在每个模型请求前复核正文。"""

    def __init__(
        self,
        repository: MemoryLifecycleRepositoryPort,
        *,
        personal_token_budget: int = 1_000,
        project_token_budget: int = 2_000,
        per_scope_limit: int = 200,
    ) -> None:
        """保存生命周期仓储及每 scope 固定预算。"""
        self._repository = repository
        self._personal_token_budget = personal_token_budget
        self._project_token_budget = project_token_budget
        self._per_scope_limit = per_scope_limit

    async def build(
        self,
        access: AccessContext,
        *,
        project_scope_key: str | None,
        tokenizer_encoding: str,
    ) -> tuple[MemoryContextItem, ...]:
        """首次读取当前 Run 固定的 always 记忆集合，后续不再补位。"""
        service = self._service(tokenizer_encoding)
        bundle = await service.build(access, project_scope_key=project_scope_key)
        return bundle.items

    async def prepare(
        self,
        access: AccessContext,
        *,
        items: tuple[MemoryContextItem, ...],
        references: tuple[MemoryReferenceKey, ...] = (),
        tokenizer_encoding: str,
        messages: tuple[RunContextMessage, ...],
        project_id: str | None,
    ) -> PreparedRunMemoryContext:
        """复核固定条目和历史 memory_search 结果，失败时移除旧正文。"""
        projected = await self._project_memory_search_messages(
            access,
            messages,
            project_id=project_id,
        )
        restored_items = items
        if not restored_items and references:
            restored_items = await self._restore_items(access, references)
        try:
            service = self._service(tokenizer_encoding)
            bundle = await service.revalidate(access, restored_items)
            content = bundle.content
            current_items = tuple(bundle.snapshot.items.values())
        except Exception:
            content = ""
            current_items = ()
        return PreparedRunMemoryContext(
            content=content,
            messages=projected,
            items=current_items,
            contains_memory=bool(content)
            or _contains_memory_search_items(projected, project_id),
        )

    def _service(self, tokenizer_encoding: str) -> MemoryContextService:
        """用当前 Run tokenizer 构造共享预算规则的上下文服务。"""
        return MemoryContextService(
            self._repository,
            RuntimeTokenCounter(tokenizer_encoding),
            personal_token_budget=self._personal_token_budget,
            project_token_budget=self._project_token_budget,
            per_scope_limit=self._per_scope_limit,
        )

    async def _restore_items(
        self,
        access: AccessContext,
        references: tuple[MemoryReferenceKey, ...],
    ) -> tuple[MemoryContextItem, ...]:
        """恢复执行只按 Run 固定引用读取正文，不再重新选择 always 候选。"""
        try:
            snapshot = await self._repository.revalidate_references(access, references)
        except Exception:
            return ()
        if not snapshot.read_enabled:
            return ()
        return tuple(
            item
            for reference in references
            if (item := snapshot.items.get(reference)) is not None
        )

    async def _project_memory_search_messages(
        self,
        access: AccessContext,
        messages: tuple[RunContextMessage, ...],
        *,
        project_id: str | None,
    ) -> tuple[RunContextMessage, ...]:
        """按 tool_call_id 识别历史检索，并只重建当前请求投影。"""
        call_names: dict[str, str] = {}
        parsed: dict[
            str,
            tuple[ToolOutcome, _MemorySearchData, tuple[MemoryReferenceKey, ...]],
        ] = {}
        references: set[MemoryReferenceKey] = set()
        for message in messages:
            if not isinstance(message, ExecutionMessage):
                continue
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    call_names[part.tool_call_id] = part.name
                elif (
                    isinstance(part, ToolResultPart)
                    and call_names.get(part.tool_call_id) == "memory_search"
                ):
                    item = _parse_memory_search(part, project_id)
                    if item is None:
                        parsed[part.tool_call_id] = (
                            _failed_outcome(),
                            _MemorySearchData(status="ok"),
                            (),
                        )
                    else:
                        outcome, data, keys = item
                        parsed[part.tool_call_id] = (outcome, data, keys)
                        references.update(keys)
        if not parsed:
            return messages
        try:
            snapshot = await self._repository.revalidate_references(
                access,
                tuple(sorted(references)),
            )
        except Exception:
            snapshot = None
        projected: list[RunContextMessage] = []
        for message in messages:
            if not isinstance(message, ExecutionMessage) or message.role is not ExecutionRole.TOOL:
                projected.append(message)
                continue
            next_parts: list[MessagePart] = []
            changed = False
            for part in message.parts:
                if not isinstance(part, ToolResultPart):
                    next_parts.append(part)
                    continue
                record = parsed.get(part.tool_call_id)
                if record is None:
                    next_parts.append(part)
                    continue
                outcome, data, keys = record
                if snapshot is None or not snapshot.read_enabled:
                    rebuilt = _replace_memory_search_output(part, _failed_outcome())
                elif outcome.status is ToolOutcomeStatus.ERROR:
                    # 历史错误结果不携带可信记忆正文；复核时也统一重建为
                    # 稳定安全错误，不能保留任何旧 data 或诊断字段。
                    rebuilt = _replace_memory_search_output(part, _failed_outcome())
                else:
                    rebuilt_data = _revalidated_search_data(data, keys, snapshot.items)
                    rebuilt_outcome = outcome.model_copy(
                        update={"data": rebuilt_data.model_dump(mode="json")}
                    )
                    rebuilt = _replace_memory_search_output(part, rebuilt_outcome)
                next_parts.append(rebuilt)
                changed = changed or rebuilt != part
            projected.append(replace(message, parts=tuple(next_parts)) if changed else message)
        return tuple(projected)


def _parse_memory_search(
    part: ToolResultPart,
    project_id: str | None,
) -> tuple[ToolOutcome, _MemorySearchData, tuple[MemoryReferenceKey, ...]] | None:
    """解析已持久化 memory_search 输出并生成服务端复核引用。"""
    if part.output is None:
        return None
    try:
        outcome = ToolOutcome.from_output(part.output)
        data = _MemorySearchData.model_validate(outcome.data)
        keys: list[MemoryReferenceKey] = []
        for item in data.items:
            scope_key = _scope_key(item.scope, project_id)
            if scope_key is None:
                return None
            keys.append((item.memory_id, item.version, scope_key))
    except (TypeError, ValueError):
        return None
    return outcome, data, tuple(keys)


def _contains_memory_search_items(
    messages: tuple[RunContextMessage, ...],
    project_id: str | None,
) -> bool:
    """识别投影后仍携带服务端记忆正文的历史检索结果。"""
    for message in messages:
        if not isinstance(message, ExecutionMessage):
            continue
        for part in message.parts:
            if not isinstance(part, ToolResultPart):
                continue
            parsed = _parse_memory_search(part, project_id)
            if parsed is not None and parsed[1].items:
                return True
    return False


def _revalidated_search_data(
    data: _MemorySearchData,
    keys: tuple[MemoryReferenceKey, ...],
    current: dict[MemoryReferenceKey, MemoryContextItem],
) -> _MemorySearchData:
    """仅用精确 ID/版本/scope 复核通过的正文重建工具结果。"""
    items: list[_MemorySearchItem] = []
    for item, key in zip(data.items, keys, strict=True):
        current_item = current.get(key)
        # ID/version 精确命中仍不足以证明正文未变；版本号相同但正文已被
        # 异常改写时必须 fail closed，不能把新正文冒充为历史检索结果。
        content = _INVALIDATED_MEMORY_TEXT
        invalidated = True
        if current_item is not None and current_item.content == item.content:
            content = current_item.content
            invalidated = False
        items.append(
            _MemorySearchItem(
                memory_id=item.memory_id,
                version=item.version,
                scope=item.scope,
                content=content,
                invalidated=invalidated,
            )
        )
    return data.model_copy(update={"items": tuple(items)})


def _replace_memory_search_output(part: ToolResultPart, outcome: ToolOutcome) -> ToolResultPart:
    """保持 tool_call_id 与协议结构，只替换当前请求中的结果正文。"""
    return replace(
        part,
        status=protocol_status_for_outcome(outcome),
        output=outcome.canonical_output(),
    )


def _failed_outcome() -> ToolOutcome:
    """构造不含任何旧正文的稳定复核失败结果。"""
    return ToolOutcome(
        status=ToolOutcomeStatus.ERROR,
        code="memory_revalidation_failed",
        message="记忆复核失败，历史检索结果正文已移除。",
        data={
            "status": "unavailable",
            "items": [],
            "truncated": False,
            "degradation_reasons": ["revalidation_failed"],
        },
    )


def _scope_key(scope: Literal["personal", "project"], project_id: str | None) -> str | None:
    """把工具结果范围别名绑定到 Run 固定项目，拒绝无项目时的 project。"""
    if scope == "personal":
        return "personal"
    return None if project_id is None else f"project:{project_id}"


__all__ = [
    "PreparedRunMemoryContext",
    "RunMemoryContextManager",
    "RuntimeTokenCounter",
]
