"""LLM 配置 ORM 到领域投影的转换。"""

from typing import Literal, cast

from adapter.llm.model.profile import ModelProfileVersionModel
from domain.llm.model.profile import ModelProfileVersion


def profile_to_domain(row: ModelProfileVersionModel) -> ModelProfileVersion:
    """构造不含凭据明文的模型版本投影。"""
    return ModelProfileVersion(
        id=row.id,
        name=row.name,
        protocol="chat_completions",
        base_url=row.base_url,
        model=row.model,
        credential_env=row.credential_env,
        context_window_tokens=row.context_window_tokens,
        tokenizer_encoding=cast(Literal["cl100k_base", "o200k_base"], row.tokenizer_encoding),
        supports_stream=row.supports_stream,
        supports_tools=row.supports_tools,
        max_output_tokens=row.max_output_tokens,
        request_timeout_seconds=row.request_timeout_seconds,
        execution_timeout_seconds=row.execution_timeout_seconds,
        max_concurrency=row.max_concurrency,
        temperature=row.temperature,
        enabled=row.enabled,
        created_at=row.created_at,
    )


__all__ = ["profile_to_domain"]
