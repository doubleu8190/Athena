"""Configured embedding provider."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import logging
from typing import Any

from .options import EmbeddingOptions, coerce_embedding_options


_LOGGER = logging.getLogger(__name__)


class EmbeddingProvider:
    def __init__(
        self, embedder: Callable[[Sequence[str]], Any], *, dimension: int
    ) -> None:
        self._embedder = embedder
        self.dimension = dimension

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        result = self._embedder(texts)
        if hasattr(result, "__await__"):
            result = await result
        return [list(map(float, value)) for value in result]

    async def embed_query(self, text: str) -> list[float]:
        values = await self.embed_documents([text])
        if not values:
            raise ValueError("embedding provider returned no vector")
        return values[0]


class ConfiguredEmbeddingProvider(EmbeddingProvider):
    def __init__(self, options: EmbeddingOptions | Any) -> None:
        self._options = coerce_embedding_options(options)
        self._loaded = False
        self._model: Any = None
        super().__init__(self._embed, dimension=self._options.dimension)

    def _load(self) -> Any:
        """根据实例配置加载 embedding 模型或远程客户端。

        参数：无显式参数；从实例保存的配置中读取 Provider 与设备。
        返回值：初始化后的模型或客户端。
        异常：ValueError 表示 Provider 不受支持；其他加载异常原样抛出。
        """
        config = self._options
        provider = config.provider.strip().lower()
        if provider in {"sentence_transformers", "sentence-transformers"}:
            from sentence_transformers import SentenceTransformer

            try:
                return SentenceTransformer(config.model, device=config.device)
            except RuntimeError as exc:
                if (
                    config.device.strip().lower() != "mps"
                    or "MPS backend is supported on macOS" not in str(exc)
                ):
                    raise
                _LOGGER.warning(
                    "MPS is unavailable for this runtime; loading embedding model on CPU"
                )
                return SentenceTransformer(config.model, device="cpu")
        if provider == "openai":
            from langchain_openai import OpenAIEmbeddings

            kwargs: dict[str, Any] = {"model": config.model, "api_key": config.api_key}
            if config.base_url:
                kwargs["base_url"] = config.base_url
            return OpenAIEmbeddings(**kwargs)
        if provider == "ollama":
            from langchain_ollama import OllamaEmbeddings

            return OllamaEmbeddings(
                model=config.model,
                base_url=config.base_url or "http://localhost:11434",
            )
        raise ValueError(f"Unsupported embedding provider: {config.provider}")

    def _embed(self, values: Sequence[str]) -> Any:
        if not self._loaded:
            self._model = self._load()
            self._loaded = True
        if hasattr(self._model, "encode"):
            encoded = self._model.encode(
                list(values), convert_to_numpy=True, normalize_embeddings=False
            )
            return [item.astype(float).tolist() for item in encoded]
        if callable(self._model) and not hasattr(self._model, "embed_documents"):
            return self._model(values)
        return self._model.embed_documents(list(values))


__all__ = ["ConfiguredEmbeddingProvider", "EmbeddingProvider"]
