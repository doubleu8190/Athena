"""本地记忆检索模型适配器。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from threading import Lock
from typing import TYPE_CHECKING, cast

from infrastructure.embedding.local import LocalEmbedding, _BoundedInference

__all__ = ["LocalEmbedding", "LocalReranker", "_BoundedInference"]

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder


class LocalReranker:
    """使用 CrossEncoder 对召回候选进行本地重排。"""

    def __init__(self, model_name: str, revision: str, device: str = "cpu") -> None:
        self.model_name = model_name
        self.revision = revision
        self.device = device
        self._model: CrossEncoder | None = None
        self._lock = Lock()
        self._inference: _BoundedInference[list[float]] = _BoundedInference()

    async def start(self) -> None:
        await asyncio.to_thread(self._load)

    def _load(self) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(
            self.model_name,
            revision=self.revision,
            device=self.device,
            local_files_only=True,
        )

    def _ensure_loaded(self) -> None:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._load()

    async def score(self, query: str, contents: list[str]) -> list[float]:
        self._ensure_loaded()
        pairs = [[query, content[:4000]] for content in contents]
        return await self._inference.run(lambda: self._predict(pairs))

    def _predict(self, pairs: list[list[str]]) -> list[float]:
        with self._lock:
            assert self._model is not None
            predict = cast(Callable[[list[list[str]]], object], self._model.predict)
            scores = cast(Sequence[float], predict(pairs))
            return [float(score) for score in scores]

    async def aclose(self) -> None:
        await self._inference.aclose()
        self._model = None
