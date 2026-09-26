from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.infrastructure.chroma.embedding import (
    ChromaEmbeddingConfig,
    collection_metadata,
    embedding_config_from_settings,
    get_or_create_collection,
)


def test_default_embedding_signature_is_stable_and_secret_free():
    config = ChromaEmbeddingConfig(
        provider="sentence_transformers",
        model="paraphrase-multilingual-MiniLM-L12-v2",
        api_key="do-not-persist",
    )

    assert "do-not-persist" not in config.signature
    assert config.signature == (
        '{"device":"cpu","model":"paraphrase-multilingual-MiniLM-L12-v2",'
        '"provider":"sentence_transformers","version":"v1"}'
    )


def test_settings_are_mapped_to_embedding_config():
    config = embedding_config_from_settings(
        SimpleNamespace(
            chroma_embedding_provider="ollama",
            chroma_embedding_model="bge-m3",
            chroma_embedding_device="cpu",
            chroma_embedding_version="2026-09",
            chroma_embedding_api_key="",
            chroma_embedding_base_url="http://localhost:11434",
        )
    )

    assert config.provider == "ollama"
    assert config.model == "bge-m3"
    assert config.version == "2026-09"
    assert collection_metadata(config)["hnsw:space"] == "cosine"


def test_collection_rejects_unsigned_existing_collection(monkeypatch: pytest.MonkeyPatch):
    config = ChromaEmbeddingConfig(provider="sentence_transformers", model="model")

    class Collection:
        metadata = {"hnsw:space": "cosine"}

    class Client:
        def get_or_create_collection(self, **kwargs):
            return Collection()

    monkeypatch.setattr(
        "athena.infrastructure.chroma.embedding.build_embedding_function",
        lambda _: object(),
    )
    with pytest.raises(RuntimeError, match="没有 embedding 签名"):
        get_or_create_collection(Client(), "athena_memory", config)
