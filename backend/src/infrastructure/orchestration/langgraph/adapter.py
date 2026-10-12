"""Compatibility import for the Root runtime adapter.

New code should import ``LangGraphRootGraph`` from ``.runtime.root_adapter``.
"""

from .runtime.root_adapter import LangGraphRootGraph

__all__ = ["LangGraphRootGraph"]
