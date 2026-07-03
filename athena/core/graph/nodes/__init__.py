"""Node implementations for the LangGraph execution graph.

Each module exports a single async node function with the signature
``async def node(state: ExecutionState, config: RunnableConfig) -> dict[str, Any]``.

The returned dict is a *partial state update* — LangGraph merges it
into the current state using the reducers declared in ``ExecutionState``.
"""

from athena.core.graph.nodes.initialize import initialize_node
from athena.core.graph.nodes.plan import plan_node
from athena.core.graph.nodes.execute import execute_node
from athena.core.graph.nodes.handle_failure import handle_failure_node
from athena.core.graph.nodes.collect import collect_node
from athena.core.graph.nodes.finalize import finalize_node

__all__ = [
    "initialize_node",
    "plan_node",
    "execute_node",
    "handle_failure_node",
    "collect_node",
    "finalize_node",
]
