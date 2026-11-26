"""文本 token 计数器：提供无状态 Tiktoken 计数能力。"""

from __future__ import annotations

import asyncio
from threading import Lock

import tiktoken

DEFAULT_TIKTOKEN_ENCODING = "cl100k_base"

__all__ = [
    "DEFAULT_TIKTOKEN_ENCODING",
    "TiktokenTokenCounter",
]


class TiktokenTokenCounter:
    """基于 tiktoken 的无状态 token 计数器，不依赖任何 HuggingFace 本地权重或缓存。"""

    def __init__(self, encoding_name: str = DEFAULT_TIKTOKEN_ENCODING) -> None:
        normalized = encoding_name.strip() if isinstance(encoding_name, str) else ""
        if not normalized:
            raise ValueError("token_counter_encoding_invalid")
        self.encoding_name = normalized
        self._encoding: tiktoken.Encoding | None = None
        self._lock = Lock()

    async def start(self) -> None:
        """在独立线程中预热编码器表，避免首次计数阻塞事件循环。"""
        await asyncio.to_thread(self._ensure_loaded)

    def _ensure_loaded(self) -> tiktoken.Encoding:
        encoding = self._encoding
        if encoding is None:
            with self._lock:
                encoding = self._encoding
                if encoding is None:
                    encoding = tiktoken.get_encoding(self.encoding_name)
                    self._encoding = encoding
        return encoding

    def encode(self, text: str) -> list[int]:
        """将纯文本编码为 tiktoken token ID 列表；特殊控制串按普通文本处理。"""
        if not text:
            return []
        encoding = self._ensure_loaded()
        # 显式关闭 disallowed_special 检查，避免用户上传文档含 <|endoftext|> 等字面量时触发异常。
        return encoding.encode(text, disallowed_special=())

    def count(self, text: str) -> int:
        """统计纯文本在 tiktoken 编码下的 token 数；特殊控制串按普通文本处理。"""
        return len(self.encode(text))

    def close(self) -> None:
        """释放编码器引用。"""
        with self._lock:
            self._encoding = None
