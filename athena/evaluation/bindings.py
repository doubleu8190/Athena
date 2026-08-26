"""将稳定的数据集别名和定位器解析为运行时 ID。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class BindingIndex:
    """保存数据集别名、运行时 ID 和文档定位之间的绑定关系。"""

    def __init__(self) -> None:
        """创建空绑定索引。"""
        self.attachment_aliases: dict[str, str] = {}
        self.memory_aliases: dict[str, str] = {}
        self.locator_to_chunk_ids: dict[str, list[str]] = {}

    def bind_attachment(self, alias: str, attachment_id: str) -> None:
        """绑定一个稳定的附件别名。

        参数：
            alias: 数据集中的附件别名。
            attachment_id: 运行时附件 ID。
        """
        self.attachment_aliases[alias] = attachment_id

    def bind_memory(self, alias: str, memory_id: str) -> None:
        """绑定一个稳定的记忆别名。

        参数：
            alias: 数据集中的记忆别名。
            memory_id: 运行时记忆 ID。
        """
        self.memory_aliases[alias] = memory_id

    def bind_locator(self, alias: str, locator_key: str, chunk_ids: list[str]) -> None:
        """记录文档定位符对应的分块 ID，并去除重复值。

        参数：
            alias: 文档别名。
            locator_key: 规范化后的定位键。
            chunk_ids: 该定位符对应的分块 ID 列表。
        """
        self.locator_to_chunk_ids[f"{alias}#{locator_key}"] = list(
            dict.fromkeys(chunk_ids)
        )

    def resolve(self, alias: str, locator_key: str | None = None) -> list[str]:
        """将数据集别名或定位符解析为运行时 ID。

        参数：
            alias: 附件或记忆别名。
            locator_key: 可选定位键；省略时解析文档或记忆本身。

        返回值：
            匹配的 ID 列表；未找到定位符时返回空列表。

        异常：
            KeyError: 省略定位键且别名未绑定到任何记忆时。
        """
        if locator_key is None:
            bound_id = self.attachment_aliases.get(alias) or self.memory_aliases.get(alias)
            return [bound_id] if bound_id else []
        return self.locator_to_chunk_ids.get(f"{alias}#{locator_key}", [])

    def as_dict(self) -> dict[str, Any]:
        """返回适合 JSON 序列化的索引字典。"""
        return {
            "attachment_aliases": self.attachment_aliases,
            "memory_aliases": self.memory_aliases,
            "locator_to_chunk_ids": self.locator_to_chunk_ids,
        }

    def write(self, path: Path) -> None:
        """将当前索引写入 JSON 文件。

        参数：
            path: 输出路径。

        异常：
            OSError: 文件无法写入。
        """
        path.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def read(cls, path: Path) -> "BindingIndex":
        """从 JSON 文件恢复绑定索引。

        参数：
            path: 已存在的索引文件路径。

        返回值：
            恢复后的 ``BindingIndex``。

        异常：
            OSError: 文件无法读取。
            json.JSONDecodeError: 文件不是有效 JSON。
        """
        value = json.loads(path.read_text(encoding="utf-8"))
        result = cls()
        result.attachment_aliases.update(value.get("attachment_aliases", {}))
        result.memory_aliases.update(value.get("memory_aliases", {}))
        result.locator_to_chunk_ids.update(value.get("locator_to_chunk_ids", {}))
        return result
