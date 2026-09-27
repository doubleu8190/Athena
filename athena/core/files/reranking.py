"""文件检索候选的 Cross-Encoder 重排实现。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from athena.core.files.contracts import FileRetrievalCandidate
from athena.core.files.ports import FileReranker


class CrossEncoderFileReranker(FileReranker):
    """使用 sentence-transformers Cross-Encoder 重排文件候选。"""

    def __init__(
        self,
        model_name: str,
        *,
        batch_size: int = 16,
        max_chars: int = 4000,
        device: str | None = None,
    ) -> None:
        """创建 reranker。

        ``device`` 传给 sentence-transformers，用于选择推理设备：``cpu``、
        ``cuda`` 或 ``mps``。留空时由 sentence-transformers/torch 自动选择。
        """
        self._model_name = model_name
        self._batch_size = batch_size
        self._max_chars = max_chars
        self._device = device
        self._model = None

    async def initialize(self) -> None:
        """在应用启动阶段加载模型，避免首个请求承担冷启动延迟。"""
        if self._model is not None:
            return

        def load_model():
            from sentence_transformers import CrossEncoder

            return CrossEncoder(self._model_name, device=self._device, max_length=512)

        self._model = await asyncio.to_thread(load_model)

    async def rerank(
        self,
        query: str,
        candidates: Sequence[FileRetrievalCandidate],
        limit: int,
    ) -> list[FileRetrievalCandidate]:
        """按 query 与 chunk 内容的交叉编码分数降序返回候选。"""
        if not candidates or limit <= 0:
            return []
        if self._model is None:
            raise RuntimeError("file reranker is not initialized")

        pairs = [[query, self._candidate_text(candidate)] for candidate in candidates]
        scores = await asyncio.to_thread(
            self._model.predict,
            pairs,
            batch_size=self._batch_size,
            show_progress_bar=False,
        )
        ranked = sorted(
            zip(candidates, scores, strict=True),
            key=lambda item: float(item[1]),
            reverse=True,
        )
        results: list[FileRetrievalCandidate] = []
        for rank, (candidate, score) in enumerate(ranked[:limit], 1):
            candidate.rerank_score = float(score)
            candidate.rerank_rank = rank
            results.append(candidate)
        return results

    def _candidate_text(self, candidate: FileRetrievalCandidate) -> str:
        title = f"文件：{candidate.source_title}\n" if candidate.source_title else ""
        return f"{title}{candidate.content}"[: self._max_chars]
