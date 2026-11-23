"""LLM 配置 ORM 到领域投影的转换。"""

from dataclasses import fields
from typing import Literal, cast

from adapter.llm.model.profile import ModelProfileVersionModel
from domain.llm.model.profile import ModelProfileVersion

_PASSTHROUGH_FIELDS = tuple(
    field.name
    for field in fields(ModelProfileVersion)
    if field.name
    not in {
        "protocol",
        "has_api_key",
        "tokenizer_encoding",
        "visible_to_users",
        "supports_vision",
    }
)


def profile_to_domain(row: ModelProfileVersionModel) -> ModelProfileVersion:
    """构造不含凭据明文的模型版本投影。"""
    values = {name: getattr(row, name) for name in _PASSTHROUGH_FIELDS}
    return ModelProfileVersion(
        protocol="chat_completions",
        has_api_key=bool(row.api_key_ciphertext),
        tokenizer_encoding=cast(Literal["cl100k_base", "o200k_base"], row.tokenizer_encoding),
        visible_to_users=True if row.visible_to_users is None else bool(row.visible_to_users),
        supports_vision=False if row.supports_vision is None else bool(row.supports_vision),
        **values,
    )


__all__ = ["profile_to_domain"]
