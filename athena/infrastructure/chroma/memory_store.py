"""长期记忆向量的 ChromaDB 适配器。"""

from __future__ import annotations

import asyncio
from typing import Any

from chromadb.api import ClientAPI


class ChromaMemoryStore:
    """表示 ChromaMemoryStore 组件，封装相关状态和行为。
    """
    def __init__(
        self,
        path: str,
        collection_name: str = "athena_memory",
    ) -> None:
        """初始化当前对象。

        参数：
            path (str): 目标文件或目录路径。
            collection_name (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._path = path
        self._client: ClientAPI | None = None
        self._collection_name = collection_name
        self._collection: Any | None = None

    @classmethod
    def with_client(
        cls,
        path: str,
        client: ClientAPI,
        collection_name: str = "athena_memory",
    ) -> "ChromaMemoryStore":
        """使用显式提供的 Chroma 客户端创建存储。"""
        store = cls(path=path, collection_name=collection_name)
        store._client = client
        return store

    async def initialize(self) -> None:
        """初始化资源。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if self._collection is not None:
            return
        if self._client is None:
            import chromadb

            self._client = chromadb.PersistentClient(path=self._path)
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def collection(self) -> Any:
        """执行“collection”操作。

        返回值：
            Any: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if self._collection is None:
            raise RuntimeError("ChromaMemoryStore is not initialized")
        return self._collection

    async def add(self, memory_id: str, content: str, metadata: dict[str, Any]) -> None:
        """添加数据。

        参数：
            memory_id (str): 记忆记录唯一标识。
            content (str): 待保存或处理的内容。
            metadata (dict[str, Any]): 附加元数据字典。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        await asyncio.to_thread(
            self.collection.add,
            ids=[memory_id],
            documents=[content],
            metadatas=[metadata],
        )

    async def query(
        self, query: str, limit: int, where: dict[str, Any] | None
    ) -> dict[str, Any]:
        """执行查询。

        参数：
            query (str): 检索或搜索文本；应为非空字符串。
            limit (int): 最大返回数量；应为非负整数。
            where (dict[str, Any] | None): 可选过滤条件。

        返回值：
            dict[str, Any]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        kwargs: dict[str, Any] = {
            "query_texts": [query],
            "n_results": limit,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        return await asyncio.to_thread(self.collection.query, **kwargs)

    async def get(self, memory_id: str) -> dict[str, Any]:
        """获取数据。

        参数：
            memory_id (str): 记忆记录唯一标识。

        返回值：
            dict[str, Any]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return await asyncio.to_thread(self.collection.get, ids=[memory_id])

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """更新数据。

        参数：
            memory_id (str): 记忆记录唯一标识。
            content (str | None): 待保存或处理的内容。
            metadata (dict[str, Any] | None): 附加元数据字典。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        kwargs: dict[str, Any] = {"ids": [memory_id]}
        if content is not None:
            kwargs["documents"] = [content]
        if metadata is not None:
            kwargs["metadatas"] = [metadata]
        await asyncio.to_thread(self.collection.update, **kwargs)

    async def delete(self, memory_ids: list[str]) -> None:
        """删除数据。

        参数：
            memory_ids (list[str]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        await asyncio.to_thread(self.collection.delete, ids=memory_ids)
