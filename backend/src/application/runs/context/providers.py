"""上下文 Provider 协议及迁移期兼容导出。

具体检索实现按业务域位于 ``files``、``memory`` 和 ``retrieval`` 包；这里
只保留上下文编排需要的协议，旧导入路径暂时继续可用。
"""

from __future__ import annotations

from typing import Any, Protocol

from .contracts import ContextPlan, ProviderResult


class ContextProvider(Protocol):
    """能为一次运行提供上下文的应用能力。"""

    name: str

    async def acquire(
        self,
        *,
        session_id: str,
        task: Any,
        plan: ContextPlan,
        run_id: str | None = None,
    ) -> ProviderResult: ...


from application.files.retrieval_service import (
    FileContextProvider,
    KnowledgeContextProvider,
)
from application.memory.retrieval_service import MemoryContextProvider
from application.retrieval.graph_service import GraphContextProvider

__all__ = [
    "ContextProvider",
    "FileContextProvider",
    "GraphContextProvider",
    "KnowledgeContextProvider",
    "MemoryContextProvider",
]
