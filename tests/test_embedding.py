from types import SimpleNamespace

from athena.infrastructure.embedding import EmbeddingConfig, embedding_config_from_settings


def test_embedding_signature_is_stable_and_secret_free():
    config = EmbeddingConfig(
        provider="sentence_transformers",
        model="BAAI/bge-m3",
        api_key="do-not-persist",
    )
    assert "do-not-persist" not in config.signature
    assert '"dimension":1024' in config.signature


def test_settings_are_mapped_to_embedding_config():
    config = embedding_config_from_settings(
        SimpleNamespace(
            embedding_provider="ollama",
            embedding_model="bge-m3",
            embedding_device="cpu",
            embedding_version="2026-09",
            embedding_dimension=1024,
            embedding_api_key="",
            embedding_base_url="http://localhost:11434",
        )
    )
    assert config.provider == "ollama"
    assert config.model == "bge-m3"
    assert config.dimension == 1024
