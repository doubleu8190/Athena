"""可由不同业务域复用的无状态远程文本嵌入与 Token 计数基础设施。"""

from infrastructure.embedding.openai_compatible import (
    EmbeddingConfigurationError,
    EmbeddingError,
    EmbeddingProtocolError,
    EmbeddingUpstreamError,
    OpenAICompatibleEmbeddingClient,
    OpenAICompatibleEmbeddingConfig,
)
from infrastructure.embedding.profile import remote_embedding_profile_id
from infrastructure.embedding.token_counter import TiktokenTokenCounter

__all__ = [
    "EmbeddingConfigurationError",
    "EmbeddingError",
    "EmbeddingProtocolError",
    "EmbeddingUpstreamError",
    "OpenAICompatibleEmbeddingClient",
    "OpenAICompatibleEmbeddingConfig",
    "TiktokenTokenCounter",
    "remote_embedding_profile_id",
]
