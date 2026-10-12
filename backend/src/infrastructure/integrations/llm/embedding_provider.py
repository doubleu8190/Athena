"""Configured embedding provider."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .options import EmbeddingOptions, coerce_embedding_options


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
        super().__init__(self._embed, dimension=self._options.dimension)

    def _load(self) -> Any:
        config = self._options
        provider = config.provider.strip().lower()
        if provider in {"sentence_transformers", "sentence-transformers"}:
            from sentence_transformers import SentenceTransformer

            return SentenceTransformer(config.model, device=config.device)
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
            self._embedder = self._load()
            self._loaded = True
        if (
            callable(self._embedder)
            and not hasattr(self._embedder, "embed_documents")
            and not hasattr(self._embedder, "encode")
        ):
            return self._embedder(values)
        if hasattr(self._embedder, "encode"):
            encoded = self._embedder.encode(
                list(values), convert_to_numpy=True, normalize_embeddings=False
            )
            return [item.astype(float).tolist() for item in encoded]
        return self._embedder.embed_documents(list(values))


__all__ = ["ConfiguredEmbeddingProvider", "EmbeddingProvider"]
