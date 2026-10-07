"""Small compatibility layer around LangGraph's StateGraph.

The production dependency is declared in ``pyproject.toml``.  The fallback is
intentionally tiny and deterministic so architecture tests can import and
exercise the graph without installing optional infrastructure dependencies.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

END = "__end__"


async def call(value: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a sync/async dependency while tolerating narrow test doubles."""
    try:
        result = value(*args, **kwargs)
    except TypeError:
        signature = inspect.signature(value)
        accepted = {
            key: item for key, item in kwargs.items() if key in signature.parameters
        }
        result = value(*args, **accepted)
    if isinstance(result, Awaitable):
        return await result
    return result


class _FallbackGraphView:
    def __init__(self, nodes: Mapping[str, Any]) -> None:
        self.nodes = dict(nodes)


class _FallbackGraph:
    def __init__(self, nodes, edges, conditional, start) -> None:
        self._nodes = nodes
        self._edges = edges
        self._conditional = conditional
        self._start = start

    def get_graph(self) -> _FallbackGraphView:
        return _FallbackGraphView(self._nodes)

    async def ainvoke(self, state: Mapping[str, Any], *, config=None) -> dict[str, Any]:
        current = self._start
        value = dict(state)
        for _ in range(128):
            if current == END:
                return value
            update = await call(self._nodes[current], value)
            if update:
                value.update(update)
            if current in self._conditional:
                router, mapping = self._conditional[current]
                route = await call(router, value)
                current = mapping.get(route, route)
            else:
                current = self._edges.get(current, END)
        raise RuntimeError("graph exceeded 128 transitions")

    async def aget_state(self, _config=None):
        return None


class GraphBuilder:
    def __init__(self, state_schema: Any) -> None:
        self._real = None
        try:
            from langgraph.graph import END as real_end
            from langgraph.graph import START as real_start
            from langgraph.graph import StateGraph

            self._real = StateGraph(state_schema)
            self.start = real_start
            self.end = real_end
        except ModuleNotFoundError:
            self.start = "__start__"
            self.end = END
        self._nodes: dict[str, Callable[..., Any]] = {}
        self._edges: dict[str, str] = {}
        self._conditional: dict[str, tuple[Callable[..., Any], dict[str, str]]] = {}

    def add_node(self, name: str, handler: Callable[..., Any]) -> None:
        self._nodes[name] = handler
        if self._real is not None:
            self._real.add_node(name, handler)

    def add_edge(self, source: str, target: str) -> None:
        self._edges[source] = target
        if self._real is not None:
            self._real.add_edge(source, target)

    def add_conditional(self, source: str, router: Callable[..., Any], mapping: dict[str, str]) -> None:
        self._conditional[source] = (router, mapping)
        if self._real is not None:
            self._real.add_conditional_edges(source, router, mapping)

    def compile(self, *, checkpointer: Any = None) -> Any:
        if self._real is not None:
            return self._real.compile(checkpointer=checkpointer)
        return _FallbackGraph(self._nodes, self._edges, self._conditional, self._edges.get(self.start, END))


__all__ = ["END", "GraphBuilder", "call"]
