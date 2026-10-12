"""Backward-compatible entry point for the Worker graph adapter."""

from .graphs.worker import build_worker_graph, invoke_worker_graph

__all__ = ["build_worker_graph", "invoke_worker_graph"]
