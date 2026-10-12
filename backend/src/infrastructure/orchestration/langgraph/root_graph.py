"""Backward-compatible entry point for the Root graph adapter."""

from .graphs.root import RootGraphDependencies, build_root_graph, invoke_root_graph

__all__ = ["RootGraphDependencies", "build_root_graph", "invoke_root_graph"]
