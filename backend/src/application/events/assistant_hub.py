"""进程内保存尚未落库的 assistant 流式文本。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Lock


@dataclass(frozen=True, slots=True)
class AssistantPrefix:
    """SSE 建连时可恢复的当前 assistant 前缀。"""

    content: str
    started_at: datetime
    completed: bool = False
    last_chunk_id: int = 0


class AssistantStreamHub:
    """按 session/run 汇总 delta，避免每次增量写数据库。"""

    def __init__(self) -> None:
        self._streams: dict[tuple[str, str], AssistantPrefix] = {}
        self._lock = Lock()

    def start(self, session_id: str, run_id: str, started_at: datetime) -> None:
        """建立本轮 assistant 前缀；重复开始不会清空已有文本。"""
        with self._lock:
            self._streams.setdefault(
                (session_id, run_id),
                AssistantPrefix(content="", started_at=started_at),
            )

    def append(
        self,
        session_id: str,
        run_id: str,
        delta: str,
        *,
        chunk_id: int,
    ) -> AssistantPrefix:
        """追加立即发送的完整 delta，并返回累计前缀。"""
        with self._lock:
            current = self._streams.get((session_id, run_id))
            if current is None:
                raise ValueError("assistant_stream_not_started")
            value = AssistantPrefix(
                content=current.content + delta,
                started_at=current.started_at,
                completed=current.completed,
                last_chunk_id=max(current.last_chunk_id, chunk_id),
            )
            self._streams[(session_id, run_id)] = value
            return value

    def complete(self, session_id: str, run_id: str) -> AssistantPrefix | None:
        """将内存前缀标记完成；数据库消息仍是最终事实。"""
        with self._lock:
            current = self._streams.get((session_id, run_id))
            if current is None:
                return None
            value = AssistantPrefix(
                content=current.content,
                started_at=current.started_at,
                completed=True,
                last_chunk_id=current.last_chunk_id,
            )
            self._streams[(session_id, run_id)] = value
            return value

    def snapshot(self, session_id: str, run_id: str) -> AssistantPrefix | None:
        """读取建连时的累计前缀。"""
        with self._lock:
            return self._streams.get((session_id, run_id))

    def clear(self, session_id: str, run_id: str) -> None:
        """Run 最终消息落库可读后释放进程内副本。"""
        with self._lock:
            self._streams.pop((session_id, run_id), None)


__all__ = ["AssistantPrefix", "AssistantStreamHub"]
