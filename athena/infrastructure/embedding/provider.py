"""Embedding providers shared by the PostgreSQL vector stores."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    model: str
    device: str = "cpu"
    version: str = "v1"
    dimension: int = 1024
    api_key: str = ""
    base_url: str = ""

    @property
    def signature(self) -> str:
        return json.dumps(
            {
                "provider": self.provider,
                "model": self.model,
                "device": self.device,
                "version": self.version,
                "dimension": self.dimension,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def embedding_config_from_settings(settings: Any) -> EmbeddingConfig:
    return EmbeddingConfig(
        provider=settings.embedding_provider,
        model=settings.embedding_model,
        device=settings.embedding_device,
        version=settings.embedding_version,
        dimension=settings.embedding_dimension or 1024,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
    )


class EmbeddingProvider:
    """Synchronous embedding facade; callers run it off the event loop."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self._function: Any | None = None

    def _load(self) -> Any:
        if self._function is not None:
            return self._function
        provider = self.config.provider.strip().lower()
        if provider in {"sentence_transformers", "sentence-transformers", "local"}:
            from sentence_transformers import SentenceTransformer

            self._function = SentenceTransformer(
                self.config.model,
                device=self.config.device,
            )
        elif provider == "openai":
            from langchain_openai import OpenAIEmbeddings

            kwargs: dict[str, Any] = {
                "model": self.config.model,
                "api_key": self.config.api_key,
            }
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            self._function = OpenAIEmbeddings(**kwargs)
        elif provider == "ollama":
            from langchain_ollama import OllamaEmbeddings

            self._function = OllamaEmbeddings(
                model=self.config.model,
                base_url=self.config.base_url or "http://localhost:11434",
            )
        else:
            raise ValueError(
                f"Unsupported EMBEDDING_PROVIDER: {self.config.provider!r}; "
                "use sentence_transformers, openai, or ollama"
            )
        return self._function

    @property
    def tokenizer(self) -> Any | None:
        # Do not load a remote model during application assembly. The file
        # runtime can use its conservative counter until the first embedding
        # operation initializes the provider.
        return getattr(self._function, "tokenizer", None)

    @property
    def dimension(self) -> int:
        return self.config.dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        if hasattr(model, "encode"):
            vectors = model.encode(
                list(texts), convert_to_numpy=True, normalize_embeddings=False
            )
            return [vector.astype(float).tolist() for vector in vectors]
        return [list(map(float, vector)) for vector in model.embed_documents(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


__all__ = ["EmbeddingConfig", "EmbeddingProvider", "embedding_config_from_settings"]
