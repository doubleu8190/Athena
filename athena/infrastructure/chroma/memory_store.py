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
        """

        参数：
            path (str): 目标文件或目录路径。
            collection_name (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
        """

        返回值：
            Any: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
            dict[str, Any]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
            dict[str, Any]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
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
            memory_ids (list[str]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        await asyncio.to_thread(self.collection.delete, ids=memory_ids)

    async def clear(self) -> int:
        """删除 collection 中的全部向量记录。

        参数：
            无。

        返回：
            删除前的向量记录数量。

        异常：
            Chroma 查询或删除失败时向上抛出异常。
        """
        records = await asyncio.to_thread(self.collection.get)
        memory_ids = [str(memory_id) for memory_id in records.get("ids", [])]
        if memory_ids:
            await asyncio.to_thread(self.collection.delete, ids=memory_ids)
        return len(memory_ids)
