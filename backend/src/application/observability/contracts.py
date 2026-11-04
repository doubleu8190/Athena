"""Trace span 与用户可见 Progress 的类型化契约。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
import re
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


_TRACE_ERROR_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_TRACE_ERROR_FIELDS = ("code", "type", "status", "phase", "category", "retryable")


def sanitize_trace_error(
    error: Mapping[str, object] | None,
) -> dict[str, str] | None:
    """把任意异常详情收缩为可持久化的 Trace 白名单字段。

    参数：
    - `error`：调用方提供的错误摘要。允许包含原始 message 等字段，但这些字段
      不会进入返回值。

    返回：
    - 不含原始文本的白名单字典；输入为空时返回 None。缺少合法 code 时使用
      `trace_error`。

    异常：
    - 无预期业务异常；非字符串或超出允许字符集的字段会被忽略。
    """
    if not error:
        return None
    cleaned: dict[str, str] = {}
    for field_name in _TRACE_ERROR_FIELDS:
        value = error.get(field_name)
        if field_name == "retryable" and isinstance(value, bool):
            cleaned[field_name] = "true" if value else "false"
            continue
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if _TRACE_ERROR_VALUE.fullmatch(normalized):
            cleaned[field_name] = normalized
    cleaned.setdefault("code", "trace_error")
    return cleaned


def run_span_id(run_id: str) -> str:
    """返回 Run 根 Trace span 的确定性标识。"""
    return f"run:{run_id}"


class ProgressSource(StrEnum):
    """Progress 的生产者；不参与前端语义分类。"""

    ROOT = "root"
    WORKER = "worker"
    ORCHESTRATOR = "orchestrator"


class ProgressAppend(BaseModel):
    """待写入的一条完整 Progress。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    execution_id: str | None = None
    task_id: str | None = None
    worker_id: str | None = None
    source: ProgressSource
    sequence: int = Field(ge=0)
    content: str = Field(min_length=1, max_length=20000)


class ProgressEntry(ProgressAppend):
    """已经落库的不可变 Progress。"""

    progress_id: str = Field(min_length=1)
    created_at: datetime
    ingest_sequence: int
    worker_name: str | None = None
    task_title: str | None = None


class WorkerProgressContext(BaseModel):
    """认证后的 Worker 执行身份，不信任请求正文中的归属字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    worker_name: str = Field(min_length=1)
    task_title: str


class _SpanAttributes(BaseModel):
    """所有 span 属性的共同严格边界。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RunSpanAttributes(_SpanAttributes):
    span_type: Literal["run"] = "run"


class RootNodeSpanAttributes(_SpanAttributes):
    span_type: Literal["root_node"] = "root_node"
    node_name: str = Field(min_length=1)


class LLMSpanAttributes(_SpanAttributes):
    span_type: Literal["llm"] = "llm"
    attempt: int = Field(ge=1)


class PlanSpanAttributes(_SpanAttributes):
    span_type: Literal["plan"] = "plan"
    plan_id: str | None = None


class WorkerExecutionSpanAttributes(_SpanAttributes):
    span_type: Literal["worker_execution"] = "worker_execution"
    execution_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    worker_id: str | None = None


class WorkerNodeSpanAttributes(_SpanAttributes):
    span_type: Literal["worker_node"] = "worker_node"
    node_name: str = Field(min_length=1)


class ToolSpanAttributes(_SpanAttributes):
    span_type: Literal["tool"] = "tool"
    tool_name: str = Field(min_length=1)
    risk_level: Literal["low", "medium", "high", "critical"] | None = None
    requires_approval: bool | None = None


class RetrievalSpanAttributes(_SpanAttributes):
    span_type: Literal["retrieval"] = "retrieval"
    stage: str | None = None
    provider: str | None = None
    candidate_count: int = Field(default=0, ge=0)


TraceSpanAttributes = Annotated[
    RunSpanAttributes
    | RootNodeSpanAttributes
    | LLMSpanAttributes
    | PlanSpanAttributes
    | WorkerExecutionSpanAttributes
    | WorkerNodeSpanAttributes
    | ToolSpanAttributes
    | RetrievalSpanAttributes,
    Field(discriminator="span_type"),
]


class TraceSpan(BaseModel):
    """可供终态链路页面读取的一条 span。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str
    session_id: str
    run_id: str
    parent_span_id: str | None = None
    span_type: str
    name: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None
    attributes: TraceSpanAttributes
    error: dict[str, str] | None = None
    sequence: int

    @field_validator("error", mode="before")
    @classmethod
    def restrict_error_fields(
        cls, value: object
    ) -> dict[str, str] | None:
        """拒绝 Trace 中的任意错误文本，只保留机器可读白名单。"""
        if value is None:
            return None
        if not isinstance(value, Mapping):
            return sanitize_trace_error({"code": "trace_error"})
        return sanitize_trace_error(value)


class ObservabilityRepository(Protocol):
    """Trace 与 Progress 的持久化端口。"""

    async def append_progress(self, value: ProgressAppend) -> ProgressEntry: ...

    async def list_progress_for_run(self, run_id: str) -> list[ProgressEntry]: ...

    async def list_progress_after(
        self, session_id: str, after_ingest_sequence: int
    ) -> list[ProgressEntry]: ...

    async def max_progress_ingest_sequence(self, session_id: str) -> int: ...

    async def progress_ingest_watermark(self) -> int: ...

    async def max_worker_progress_sequence(self, execution_id: str) -> int: ...

    async def append_worker_progress(
        self,
        context: WorkerProgressContext,
        *,
        sequence: int,
        content: str,
    ) -> ProgressEntry: ...

    async def worker_progress_context(
        self, *, execution_id: str, worker_id: str
    ) -> WorkerProgressContext | None: ...

    async def find_progress(
        self, *, execution_id: str, sequence: int
    ) -> ProgressEntry | None: ...

    async def start_span(
        self,
        *,
        span_id: str,
        session_id: str,
        run_id: str,
        parent_span_id: str | None,
        name: str,
        started_at: datetime,
        attributes: TraceSpanAttributes,
    ) -> TraceSpan: ...

    async def finish_span(
        self,
        *,
        span_id: str,
        status: str,
        completed_at: datetime,
        duration_ms: int,
        error: dict[str, str] | None = None,
    ) -> None: ...

    async def list_spans_for_run(self, run_id: str) -> list[TraceSpan]: ...


__all__ = [
    "LLMSpanAttributes",
    "ObservabilityRepository",
    "PlanSpanAttributes",
    "ProgressAppend",
    "ProgressEntry",
    "ProgressSource",
    "RetrievalSpanAttributes",
    "RootNodeSpanAttributes",
    "RunSpanAttributes",
    "ToolSpanAttributes",
    "TraceSpan",
    "TraceSpanAttributes",
    "WorkerExecutionSpanAttributes",
    "WorkerNodeSpanAttributes",
    "WorkerProgressContext",
    "sanitize_trace_error",
    "run_span_id",
]
