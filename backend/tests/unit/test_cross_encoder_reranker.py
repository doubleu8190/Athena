"""Cross-Encoder 输入构造与并发边界测试。"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from domain.retrieval import RetrievalCandidate
from infrastructure.integrations.reranking import CrossEncoderReranker


class _FakeReranker(CrossEncoderReranker):
    def __init__(self, model, **kwargs) -> None:
        super().__init__("fake-reranker", **kwargs)
        self._fake_model = model

    def _ensure_loaded(self):
        return self._fake_model


@pytest.mark.asyncio
async def test_cross_encoder_pairs_include_source_title() -> None:
    """文件来源标题应参与交叉编码，并与正文共享字符上限。"""

    class RecordingModel:
        def __init__(self) -> None:
            self.pairs: list[list[str]] = []

        def predict(self, pairs, **_kwargs):
            self.pairs = [list(pair) for pair in pairs]
            return [0.8]

    model = RecordingModel()
    reranker = _FakeReranker(model, max_chars=24)
    candidate = RetrievalCandidate(
        "chunk-1",
        "body that is truncated",
        "file",
        0.9,
        {"source_metadata": {"title": "Quarterly Report"}},
    )

    await reranker.rerank("revenue", [candidate], limit=1)

    assert model.pairs[0] == [
        "revenue",
        "Quarterly Report\nbody th",
    ]


@pytest.mark.asyncio
async def test_cross_encoder_serializes_sync_predict_calls() -> None:
    """共享 reranker 的多个协程不得并发进入 model.predict。"""

    class BlockingModel:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def predict(self, _pairs, **_kwargs):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.05)
            with self.lock:
                self.active -= 1
            return [0.5]

    model = BlockingModel()
    reranker = _FakeReranker(model)
    candidate = RetrievalCandidate("chunk-1", "content")

    await asyncio.gather(
        reranker.rerank("query", [candidate], limit=1),
        reranker.rerank("query", [candidate], limit=1),
    )

    assert model.max_active == 1


@pytest.mark.asyncio
async def test_cross_encoder_rejects_excess_waiting_inference_calls() -> None:
    """推理等待队列超过上限时立即报错，供检索层回退。"""

    class BlockingModel:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def predict(self, _pairs, **_kwargs):
            self.started.set()
            self.release.wait(timeout=2)
            return [0.5]

    model = BlockingModel()
    reranker = _FakeReranker(model)
    candidate = RetrievalCandidate("chunk-1", "content")
    tasks = [
        asyncio.create_task(reranker.rerank("query", [candidate], limit=1))
        for _ in range(5)
    ]

    assert await asyncio.to_thread(model.started.wait, 1)
    await asyncio.sleep(0.05)
    model.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    capacity_errors = [
        result
        for result in results
        if isinstance(result, RuntimeError)
        and str(result) == "reranker_inference_capacity_exceeded"
    ]
    assert len(capacity_errors) == 1


@pytest.mark.asyncio
async def test_canceled_inference_holds_slot_until_sync_predict_finishes() -> None:
    """协程取消后，仍在运行的线程继续占用名额并串行执行 predict。"""

    class BlockingModel:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def predict(self, _pairs, **_kwargs):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            self.started.set()
            self.release.wait(timeout=2)
            with self.lock:
                self.active -= 1
            return [0.5]

    model = BlockingModel()
    reranker = _FakeReranker(model)
    candidate = RetrievalCandidate("chunk-1", "content")
    first = asyncio.create_task(reranker.rerank("query", [candidate], limit=1))

    assert await asyncio.to_thread(model.started.wait, 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    tasks = [
        asyncio.create_task(reranker.rerank("query", [candidate], limit=1))
        for _ in range(4)
    ]
    await asyncio.sleep(0.05)
    model.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert model.max_active == 1
    assert any(
        isinstance(result, RuntimeError)
        and str(result) == "reranker_inference_capacity_exceeded"
        for result in results
    )
