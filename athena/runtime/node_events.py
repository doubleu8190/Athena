"""LangGraph 节点生命周期事件适配器。

图节点本身只负责业务状态转换；本模块在图注册边界统一发布节点开始、完成和失败
事件，避免每个节点重复编写相同的观测代码，也保证异常路径不会遗漏失败事件。
"""

from __future__ import annotations

import inspect
import time
import traceback
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from uuid import uuid4

from langchain_core.runnables import RunnableConfig

from athena.contracts.errors import ExecutionError
from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.ports import EventPublisherPort
from athena.utils.logging import get_logger
from athena.observability.langsmith import finish_span, trace_span

logger = get_logger(__name__)

NodeCallable = Callable[..., Any]


def instrument_graph_node(
    node_name: str,
    node: NodeCallable,
    *,
    event_publisher: EventPublisherPort | None,
) -> NodeCallable:
    """为一个 LangGraph 节点增加开始、完成和失败事件。

    参数：
        node_name: 图中注册的稳定节点名，用于前端展示和日志检索。
        node: 原始节点函数或可执行的 LangGraph 子图对象。
        event_publisher: Durable Application Event 发布器；测试或离线构图场景
            可以传入 ``None``，此时返回原节点，不改变业务执行。
    返回值：
        NodeCallable：可直接传给 ``StateGraph.add_node`` 的异步包装节点。
    异常：
        原节点抛出的异常会在发布失败事件后原样重新抛出；事件发布异常会被
        记录并吞掉，避免观测故障改变任务执行结果。
    """
    if event_publisher is None:
        return node

    async def wrapped_node(
        state: Any,
        config: RunnableConfig,
    ) -> Any:
        execution_id = uuid4().hex
        session_id, run_id, parent_run_id = _resolve_execution_identity(state, config)
        started_at = time.monotonic()
        trace = None
        await _publish_node_event(
            event_publisher,
            event_type=EventType.NODE_STARTED,
            node_name=node_name,
            execution_id=execution_id,
            session_id=session_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
        )
        try:
            async with trace_span(
                name=f"node.{node_name}",
                run_type="chain",
                inputs={"session_id": session_id, "run_id": run_id, "node": node_name},
                run_id=f"{run_id}:node:{node_name}:{execution_id}",
                metadata={"session_id": session_id, "run_id": run_id, "node_name": node_name},
                tags=["athena", "node", node_name],
            ) as trace:
                result = await _invoke_node(node, state, config)
                finish_span(trace, outputs={"status": "completed", "duration_ms": _duration_ms(started_at)})
        except BaseException as exc:
            finish_span(trace, error=str(exc))
            detail = ExecutionError.from_exception(
                exc,
                code="graph_node_failed",
                retryable=False,
                phase=f"langgraph.node.{node_name}",
                category="langgraph_node",
                stack=traceback.format_exc(),
            )
            await _publish_node_event(
                event_publisher,
                event_type=EventType.NODE_FAILED,
                node_name=node_name,
                execution_id=execution_id,
                session_id=session_id,
                run_id=run_id,
                parent_run_id=parent_run_id,
                duration_ms=_duration_ms(started_at),
                error_detail=detail,
            )
            raise

        await _publish_node_event(
            event_publisher,
            event_type=EventType.NODE_COMPLETED,
            node_name=node_name,
            execution_id=execution_id,
            session_id=session_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
            duration_ms=_duration_ms(started_at),
        )
        return result

    wrapped_node.__name__ = f"instrumented_{node_name.replace('-', '_')}"
    wrapped_node.__qualname__ = wrapped_node.__name__
    return wrapped_node


async def _invoke_node(
    node: NodeCallable,
    state: Any,
    config: RunnableConfig | None,
) -> Any:
    """调用普通节点函数或编译后的 LangGraph 子图。"""
    ainvoke = getattr(node, "ainvoke", None)
    if callable(ainvoke):
        invoke_async = cast(Callable[..., Awaitable[Any]], ainvoke)
        return await invoke_async(state, config=config)

    if config is not None and _accepts_config(node):
        result = node(state, config)
    else:
        result = node(state)
    if inspect.isawaitable(result):
        return await cast(Awaitable[Any], result)
    return result


def _accepts_config(node: NodeCallable) -> bool:
    """判断普通节点函数是否声明了 LangGraph 配置参数。"""
    try:
        parameters = inspect.signature(node).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "config"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _resolve_execution_identity(
    state: Any,
    config: RunnableConfig | None,
) -> tuple[str, str | None, str | None]:
    """从节点状态和 LangGraph 配置中解析事件归属。"""
    state_mapping = state if isinstance(state, Mapping) else {}
    configurable = _mapping_value(config, "configurable")
    metadata = _mapping_value(config, "metadata")
    execution_state = state_mapping.get("execution")
    execution_mapping = (
        execution_state if isinstance(execution_state, Mapping) else {}
    )
    session_id = _first_text(
        state_mapping.get("session_id"),
        execution_mapping.get("session_id"),
        metadata.get("session_id"),
        configurable.get("session_id"),
    )
    run_id = _first_text(
        state_mapping.get("run_id"),
        execution_mapping.get("run_id"),
        metadata.get("run_id"),
        configurable.get("run_id"),
        configurable.get("thread_id"),
    ) or None
    parent_run_id = _first_text(
        state_mapping.get("parent_run_id"),
        execution_mapping.get("parent_run_id"),
    ) or None
    return session_id, run_id, parent_run_id


def _mapping_value(value: Any, key: str) -> Mapping[str, Any]:
    """从动态 LangGraph 配置中安全读取一层映射。"""
    if not isinstance(value, Mapping):
        return {}
    nested = value.get(key)
    return nested if isinstance(nested, Mapping) else {}


def _first_text(*values: Any) -> str:
    """返回第一个非空字符串值。"""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _duration_ms(started_at: float) -> float:
    """将单调时钟起点转换为毫秒耗时。"""
    return round((time.monotonic() - started_at) * 1000, 3)


async def _publish_node_event(
    event_publisher: EventPublisherPort,
    *,
    event_type: EventType,
    node_name: str,
    execution_id: str,
    session_id: str,
    run_id: str | None,
    parent_run_id: str | None,
    duration_ms: float | None = None,
    error_detail: ExecutionError | None = None,
) -> None:
    """发布一个节点生命周期事件；观测失败不阻断业务节点。"""
    if not session_id:
        logger.warning(
            "graph_node_event_skipped_missing_session",
            node_name=node_name,
            event_type=event_type.value,
            run_id=run_id,
        )
        return

    payload: dict[str, Any] = {
        "node_name": node_name,
        "execution_id": execution_id,
        "status": (
            "started"
            if event_type is EventType.NODE_STARTED
            else "failed"
            if event_type is EventType.NODE_FAILED
            else "completed"
        ),
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if error_detail is not None:
        payload["error"] = error_detail.message
        payload["error_detail"] = error_detail.model_dump(mode="json")

    event = ApplicationEvent(
        event_type=event_type,
        durability=EventDurability.DURABLE,
        session_id=session_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        transition_id=f"node:{run_id or 'unknown'}:{node_name}:{execution_id}:{event_type.value}",
        payload=payload,
    )
    try:
        await event_publisher.publish(event)
    except Exception:
        logger.warning(
            "graph_node_event_publish_failed",
            node_name=node_name,
            event_type=event_type.value,
            run_id=run_id,
            exc_info=True,
        )
