"""基于 Cross-Encoder 的检索候选重排实现。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from typing import Any

from domain.retrieval import (
    RetrievalCandidate,
    RetrievalRerankPort,
    RetrievalRerankResult,
)
from shared.logging import get_logger

logger = get_logger(__name__)

_STARTUP_LOAD_TIMEOUT_SECONDS = 5.0
_MAX_INFERENCE_WAITERS = 3
_MAX_INFERENCE_CALLERS = _MAX_INFERENCE_WAITERS + 1


class CrossEncoderReranker(RetrievalRerankPort):
    """使用 sentence-transformers Cross-Encoder 对候选做有界重排。

    模型延迟加载且只加载一次。加载或推理失败时直接向上抛出，由检索模块
    回退到融合顺序；因此重排能力不可用不会阻断基础召回。
    """

    def __init__(
        self,
        model_name: str,
        *,
        device: str | None = None,
        batch_size: int = 16,
        max_chars: int = 4000,
    ) -> None:
        """保存模型配置；构造对象时不加载模型。

        参数：
        - `model_name`（str）：Cross-Encoder 模型名称或本地路径。
        - `device`（str | None）：推理设备；None 表示交给运行时自动选择。
        - `batch_size`（int）：单次推理批大小。
        - `max_chars`（int）：每个候选参与交叉编码的最大字符数。

        返回：
        - None：构造完成后不提供返回数据。

        异常：
        - ValueError：模型名为空或批大小、字符上限非法。
        """
        if not model_name.strip():
            raise ValueError("reranker_model_required")
        if batch_size < 1:
            raise ValueError("reranker_batch_size_invalid")
        if max_chars < 1:
            raise ValueError("reranker_max_chars_invalid")
        self._model_name = model_name
        self._device = device
        self._batch_size = batch_size
        self._max_chars = max_chars
        self._model: Any = None
        self._load_lock = threading.Lock()
        self._predict_lock = threading.Lock()
        self._inference_slot_lock = threading.Lock()
        self._inference_thread_slots = 0
        self._inference_gate: asyncio.Lock | None = None

    async def start(self) -> None:
        """在应用启动阶段有界预热模型；失败只记录日志，不阻断应用启动。"""
        try:
            async with asyncio.timeout(_STARTUP_LOAD_TIMEOUT_SECONDS):
                await asyncio.to_thread(self._ensure_loaded)
        except TimeoutError:
            logger.warning(
                "reranker_startup_load_timed_out",
                model=self._model_name,
                timeout_seconds=_STARTUP_LOAD_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("reranker_startup_load_failed", model=self._model_name)

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievalCandidate],
        *,
        limit: int,
    ) -> list[RetrievalRerankResult]:
        """按查询与候选标题及正文的交叉编码分数降序返回有界结果。

        参数：
        - `query`（str）：用户查询或改写后的检索查询。
        - `candidates`（Sequence[RetrievalCandidate]）：已经过融合的候选。
        - `limit`（int）：最多返回的候选数量；非正值返回空列表。

        返回：
        - list[RetrievalRerankResult]：带模型分数和稳定名次的候选。

        异常：
        - ValueError：查询为空。
        - 模型加载或推理失败时原样抛出，由调用方执行降级。
        """
        if not query.strip():
            raise ValueError("reranker_query_required")
        if not candidates or limit <= 0:
            return []
        pairs = [
            [query, self._candidate_text(candidate)]
            for candidate in candidates[:limit]
        ]
        scores = await self._predict_bounded(pairs)
        ordered = sorted(
            zip(candidates[:limit], scores, strict=True),
            key=lambda item: (-float(item[1]), item[0].source_id),
        )
        return [
            RetrievalRerankResult(candidate, float(score), rank)
            for rank, (candidate, score) in enumerate(ordered, 1)
        ]

    def _candidate_text(self, candidate: RetrievalCandidate) -> str:
        """拼接来源标题与正文，并按配置字符上限截断。"""
        source_metadata = candidate.metadata.get("source_metadata")
        title = (
            source_metadata.get("title")
            if isinstance(source_metadata, dict)
            else None
        )
        normalized_title = str(title).strip() if title is not None else ""
        text = (
            f"{normalized_title}\n{candidate.content}"
            if normalized_title
            else candidate.content
        )
        return text[: self._max_chars]

    async def _predict_bounded(self, pairs: list[list[str]]) -> Any:
        """限制并发调用者数量，并在同步线程内串行执行模型推理。"""
        self._reserve_inference_slot()
        submitted = False
        try:
            gate = self._inference_gate
            if gate is None:
                gate = asyncio.Lock()
                self._inference_gate = gate
            async with gate:
                submitted = True
                worker = asyncio.create_task(
                    asyncio.to_thread(self._load_and_predict, pairs)
                )
                worker.add_done_callback(_consume_task_exception)
                return await asyncio.shield(worker)
        finally:
            if not submitted:
                self._release_inference_slot()

    def _reserve_inference_slot(self) -> None:
        """在提交线程任务前占用有界名额，队列满时立即拒绝。"""
        with self._inference_slot_lock:
            if self._inference_thread_slots >= _MAX_INFERENCE_CALLERS:
                raise RuntimeError("reranker_inference_capacity_exceeded")
            self._inference_thread_slots += 1

    def _release_inference_slot(self) -> None:
        """同步线程结束后释放名额；协程取消时也不会提前释放。"""
        with self._inference_slot_lock:
            self._inference_thread_slots -= 1

    def _load_and_predict(self, pairs: list[list[str]]) -> Any:
        """在同一受控线程内完成加载与推理，并在真正结束后释放名额。"""
        try:
            model = self._ensure_loaded()
            return self._predict_sync(model, pairs)
        finally:
            self._release_inference_slot()

    def _predict_sync(self, model: Any, pairs: list[list[str]]) -> Any:
        """在线程锁内调用 predict，保证取消或超时后也不会并行推理。"""
        with self._predict_lock:
            return model.predict(
                pairs,
                batch_size=self._batch_size,
                show_progress_bar=False,
            )

    def _ensure_loaded(self) -> Any:
        """单飞加载模型，并兼容 macOS 上不可用的 MPS 后端。"""
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            from sentence_transformers import CrossEncoder

            try:
                self._model = CrossEncoder(
                    self._model_name,
                    device=self._device,
                    max_length=512,
                )
            except RuntimeError as exc:
                requested_device = (self._device or "").strip().lower()
                if (
                    requested_device != "mps"
                    or "MPS backend is supported on macOS" not in str(exc)
                ):
                    raise
                logger.warning(
                    "reranker_mps_unavailable_using_cpu",
                    model=self._model_name,
                    requested_device=self._device,
                )
                self._model = CrossEncoder(
                    self._model_name,
                    device="cpu",
                    max_length=512,
                )
            return self._model


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """读取后台推理任务的异常，避免调用方取消后出现未消费告警。"""
    if not task.cancelled():
        task.exception()


__all__ = ["CrossEncoderReranker"]
