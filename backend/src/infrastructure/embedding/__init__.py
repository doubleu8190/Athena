"""可由不同业务域复用的文本嵌入与 Token 计数基础设施。"""

from infrastructure.embedding.local import LocalEmbedding
from infrastructure.embedding.openai_compatible import (
    EmbeddingConfigurationError,
    EmbeddingError,
    EmbeddingProtocolError,
    EmbeddingUpstreamError,
    OpenAICompatibleEmbeddingClient,
    OpenAICompatibleEmbeddingConfig,
)
from infrastructure.embedding.profile import embedding_profile_id, remote_embedding_profile_id
from infrastructure.embedding.token_counter import (
    ApproximateTokenCounter,
    LocalTokenCounter,
    TiktokenTokenCounter,
)

__all__ = [
    "ApproximateTokenCounter",
    "EmbeddingConfigurationError",
    "EmbeddingError",
    "EmbeddingProtocolError",
    "EmbeddingUpstreamError",
    "LocalEmbedding",
    "LocalTokenCounter",
    "OpenAICompatibleEmbeddingClient",
    "OpenAICompatibleEmbeddingConfig",
    "TiktokenTokenCounter",
    "embedding_profile_id",
    "remote_embedding_profile_id",
]
