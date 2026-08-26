"""用于无内容影子记录和反馈记录的小型追加式存储。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from athena.evaluation.redaction import sanitize_event


class JsonlEventStore:
    """只追加写入、逐行读取的 JSONL 事件存储。"""

    def __init__(self, path: Path) -> None:
        """创建事件存储并确保父目录存在。

        参数：
            path: JSONL 文件路径。
        """
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        """脱敏并追加一条事件。

        参数：
            event: JSON 兼容事件字典。

        异常：
            ValueError: 事件包含禁止持久化的内容字段。
            OSError: 文件无法写入。
        """
        safe = sanitize_event(dict(event))
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")

    def read(self) -> Iterator[dict[str, Any]]:
        """逐行读取已保存事件；文件不存在时不产生任何值。

        生成值：
            已解析的事件字典。

        异常：
            OSError: 文件无法读取。
            json.JSONDecodeError: 某行不是有效 JSON。
        """
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)
