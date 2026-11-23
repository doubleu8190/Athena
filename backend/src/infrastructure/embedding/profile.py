"""Stable identifier for an embedding model and preprocessing contract."""

from __future__ import annotations

import hashlib

MAX_PROFILE_ID_LENGTH = 128


def remote_embedding_profile_id(model: str, dimensions: int) -> str:
    """由远程 OpenAI 兼容模型名与向量维度生成稳定的知识库档案标识。

    确保 API、Run Worker 与 Knowledge Worker 产出完全一致的 profile_id，
    且长度始终满足 PostgreSQL knowledge_document_versions.profile_id (128 字符) 约束。
    """
    normalized_model = model.strip()
    if not normalized_model:
        raise ValueError("embedding_model_invalid")
    if isinstance(dimensions, bool) or dimensions < 1:
        raise ValueError("embedding_dimensions_invalid")
    candidate = f"openai_compatible:{normalized_model}:{dimensions}"
    if len(candidate) <= MAX_PROFILE_ID_LENGTH:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:32]
    return f"openai_compatible:sha256:{digest}:{dimensions}"


__all__ = ["MAX_PROFILE_ID_LENGTH", "remote_embedding_profile_id"]
