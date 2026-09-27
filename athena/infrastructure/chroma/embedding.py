"""Chroma embedding 配置、构造和 collection 兼容性检查。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChromaEmbeddingConfig:
    """一个可持久化到 collection metadata 的 embedding 配置。"""

    provider: str
    model: str
    device: str = "cpu"
    version: str = "v1"
    api_key: str = ""
    base_url: str = ""

    @property
    def signature(self) -> str:
        """返回不包含密钥的稳定配置签名。"""
        return json.dumps(
            {
                "provider": self.provider,
                "model": self.model,
                "device": self.device,
                "version": self.version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def embedding_config_from_settings(settings: Any) -> ChromaEmbeddingConfig:
    """从应用 Settings 创建 embedding 配置。"""
    return ChromaEmbeddingConfig(
        provider=settings.chroma_embedding_provider,
        model=settings.chroma_embedding_model,
        device=settings.chroma_embedding_device,
        version=settings.chroma_embedding_version,
        api_key=settings.chroma_embedding_api_key,
        base_url=settings.chroma_embedding_base_url,
    )


def build_embedding_function(config: ChromaEmbeddingConfig) -> Any:
    """按配置构造 Chroma embedding function。

    第三方模型在首次使用时才导入，避免仅运行测试或管理命令时加载模型。
    """
    provider = config.provider.strip().lower()
    if provider in {"sentence_transformers", "sentence-transformers", "local"}:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

        return SentenceTransformerEmbeddingFunction(
            model_name=config.model,
            device=config.device,
        )
    if provider == "openai":
        from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

        kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "model_name": config.model,
        }
        if config.base_url:
            kwargs["api_base"] = config.base_url
        return OpenAIEmbeddingFunction(**kwargs)
    if provider == "ollama":
        from chromadb.utils.embedding_functions import OllamaEmbeddingFunction

        return OllamaEmbeddingFunction(
            url=config.base_url or "http://localhost:11434",
            model_name=config.model,
        )
    raise ValueError(
        "Unsupported CHROMA_EMBEDDING_PROVIDER: "
        f"{config.provider!r}; use sentence_transformers, openai, or ollama"
    )


def collection_metadata(config: ChromaEmbeddingConfig) -> dict[str, Any]:
    """生成 collection metadata，并保留 Chroma 的 cosine 配置。"""
    return {
        "hnsw:space": "cosine",
        "athena:embedding_provider": config.provider,
        "athena:embedding_model": config.model,
        "athena:embedding_device": config.device,
        "athena:embedding_version": config.version,
        "athena:embedding_signature": config.signature,
    }


def get_or_create_collection(
    client: Any,
    name: str,
    config: ChromaEmbeddingConfig,
    *,
    embedding_function: Any | None = None,
) -> Any:
    """创建 collection，并拒绝与当前模型不兼容的历史向量。"""
    if embedding_function is None:
        embedding_function = build_embedding_function(config)
    metadata = collection_metadata(config)
    collection = client.get_or_create_collection(
        name=name,
        metadata=metadata,
        embedding_function=embedding_function,
    )

    stored_signature = (getattr(collection, "metadata", None) or {}).get(
        "athena:embedding_signature"
    )
    if stored_signature is not None and stored_signature != config.signature:
        raise RuntimeError(
            f"Chroma collection {name!r} 使用了不同的 embedding 配置；"
            "请先删除该 collection 后再重建向量。"
        )
    if stored_signature is None and getattr(collection, "metadata", None):
        raise RuntimeError(
            f"Chroma collection {name!r} 没有 embedding 签名，可能包含旧默认向量；"
            "请先删除该 collection 后再重建向量。"
        )
    return collection
