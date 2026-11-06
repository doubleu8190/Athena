"""本地记忆检索模型适配器。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from threading import Lock
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder, SentenceTransformer


class _BoundedInference[ResultT]:
    """限制本地模型推理的活动与排队请求数。"""

    def __init__(self, capacity: int = 1, max_waiting: int = 8) -> None:
        self._capacity = asyncio.Queue[None](capacity)
        for _ in range(capacity):
            self._capacity.put_nowait(None)
        self._max_admitted = capacity + max_waiting
        self._admitted = 0
        self._guard = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()
        self._tasks: set[asyncio.Task[ResultT]] = set()
        self._closed = False

    async def run(self, operation: Callable[[], ResultT]) -> ResultT:
        # 先限制活动与等待总数，避免无限协程排队在 semaphore 内部。
        async with self._guard:
            if self._closed:
                raise RuntimeError("memory_inference_closed")
            if self._admitted >= self._max_admitted:
                raise RuntimeError("memory_inference_overloaded")
            self._admitted += 1
            self._idle.clear()
        reserved = True
        acquired = False
        try:
            await self._capacity.get()
            acquired = True
            if self._closed:
                raise RuntimeError("memory_inference_closed")
            task: asyncio.Task[ResultT] = asyncio.create_task(asyncio.to_thread(operation))
        except asyncio.CancelledError:
            if reserved:
                self._release_admission(acquired)
            raise
        except RuntimeError:
            if reserved:
                self._release_admission(acquired)
            raise

        reserved = False
        self._tasks.add(task)
        task.add_done_callback(lambda finished: self._finish(finished))
        return await asyncio.shield(task)

    def _release_admission(self, acquired: bool) -> None:
        if acquired:
            self._capacity.put_nowait(None)
        self._admitted -= 1
        if self._admitted == 0:
            self._idle.set()

    def _finish(self, task: asyncio.Task[ResultT]) -> None:
        # 子线程完成后才归还推理容量；请求取消时不会等待不可中断的线程。
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        self._tasks.discard(task)
        self._capacity.put_nowait(None)
        self._admitted -= 1
        if self._admitted == 0:
            self._idle.set()

    async def aclose(self) -> None:
        async with self._guard:
            self._closed = True
        await self._idle.wait()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


class LocalEmbedding:
    """使用本地 SentenceTransformer 同步生成嵌入向量。"""

    def __init__(self, model_name: str, revision: str, device: str = "cpu") -> None:
        self.model_name = model_name
        self.revision = revision
        self.device = device
        self._model: SentenceTransformer | None = None
        self._lock = Lock()
        self._inference: _BoundedInference[list[list[float]]] = _BoundedInference()

    async def start(self) -> None:
        await asyncio.to_thread(self._load)

    def _load(self) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(
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

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure_loaded()
        return await self._inference.run(lambda: self._encode(texts))

    def _encode(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            assert self._model is not None
            vectors = cast(
                Sequence[Sequence[float]],
                self._model.encode(texts, normalize_embeddings=False),
            )
            return [[float(value) for value in row] for row in vectors]

    async def aclose(self) -> None:
        await self._inference.aclose()
        self._model = None


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


class LocalTokenCounter:
    """使用 BGE tokenizer 统计预算单位。"""

    def __init__(self, model_name: str, revision: str) -> None:
        self.model_name = model_name
        self.revision = revision
        self._tokenizer = None

    def _load(self) -> None:
        from transformers import AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_name, revision=self.revision, local_files_only=True
        )  # type: ignore[no-untyped-call]

    async def start(self) -> None:
        await asyncio.to_thread(self._load)

    def close(self) -> None:
        self._tokenizer = None

    def count(self, text: str) -> int:
        if self._tokenizer is None:
            self._load()
        assert self._tokenizer is not None
        return len(self._tokenizer.encode(text, add_special_tokens=False))
