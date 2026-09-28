"""Provider-independent text embedding construction."""

from .provider import EmbeddingConfig, EmbeddingProvider, embedding_config_from_settings

__all__ = ["EmbeddingConfig", "EmbeddingProvider", "embedding_config_from_settings"]
