"""Deterministic Chinese search tokenization."""

from __future__ import annotations

import asyncio
import re
import threading
from typing import Any


def _import_jieba() -> Any:
    """延迟导入可选依赖 jieba，避免模块导入期强制触发依赖加载。"""
    import jieba  # type: ignore[import-untyped]

    return jieba


class JiebaChineseTokenizer:
    """Use search-mode Chinese terms and preserve identifiers and versions."""

    _identifier = re.compile(r"[A-Za-z][A-Za-z0-9_.:/+-]*|\d+(?:\.\d+)+")

    def __init__(self) -> None:
        self._warmup_lock = threading.Lock()
        self._warmed = False

    def warmup(self) -> None:
        """幂等初始化 jieba 分词词典，避免首个在线检索请求触发同步冷加载阻塞。"""
        if self._warmed:
            return
        with self._warmup_lock:
            if self._warmed:
                return
            try:
                jieba = _import_jieba()
            except ImportError:
                # 可选依赖未安装时安全跳过预热，实际调用 tokenize 时仍按缺失依赖显式报错。
                return
            jieba.initialize()
            self._warmed = True

    async def start(self) -> None:
        """在工作线程中异步预热词典，供进程启动生命周期调用。"""
        await asyncio.to_thread(self.warmup)

    def tokenize(self, text: str) -> tuple[str, ...]:
        try:
            jieba = _import_jieba()
        except ImportError as error:
            raise RuntimeError("knowledge_tokenizer_dependency_missing") from error
        words = (word.strip().lower() for word in jieba.cut_for_search(text))
        tokens = [word for word in words if word]
        tokens.extend(match.group(0).lower() for match in self._identifier.finditer(text))
        return tuple(dict.fromkeys(tokens))
