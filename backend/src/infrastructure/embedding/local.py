"""本地 SentenceTransformer embedding 及有界推理资源。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from threading import Lock
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


class _BoundedInference[ResultT]:
    """限制模型推理的活动与排队请求数量。"""

    def __init__(
        self, capacity: int = 1, max_waiting: int = 8, *, error_prefix: str = "memory"
    ) -> None:
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
        self._error_prefix = error_prefix

    async def run(self, operation: Callable[[], ResultT]) -> ResultT:
        async with self._guard:
            if self._closed:
                raise RuntimeError(f"{self._error_prefix}_inference_closed")
            if self._admitted >= self._max_admitted:
                raise RuntimeError(f"{self._error_prefix}_inference_overloaded")
            self._admitted += 1
            self._idle.clear()
        reserved = True
        acquired = False
        try:
            await self._capacity.get()
            acquired = True
            if self._closed:
                raise RuntimeError(f"{self._error_prefix}_inference_closed")
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
        task.add_done_callback(self._finish)
        return await asyncio.shield(task)

    def _release_admission(self, acquired: bool) -> None:
        if acquired:
            self._capacity.put_nowait(None)
        self._admitted -= 1
        if self._admitted == 0:
            self._idle.set()

    def _finish(self, task: asyncio.Task[ResultT]) -> None:
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
    """使用固定本地 SentenceTransformer revision 生成向量。"""

    def __init__(self, model_name: str, revision: str, device: str = "cpu") -> None:
        self.model_name = model_name
        self.revision = revision
        self.device = device
        self._model: SentenceTransformer | None = None
        self._lock = Lock()
        self._inference: _BoundedInference[list[list[float]]] = _BoundedInference(
            error_prefix="embedding"
        )

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

    def _load_if_needed(self) -> None:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._load()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            await asyncio.to_thread(self._load_if_needed)
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
