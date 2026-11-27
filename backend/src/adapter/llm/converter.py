"""LLM 配置 ORM 到领域投影的转换。"""

from dataclasses import fields
from typing import Literal, cast

from adapter.llm.model import ModelProfileVersionModel
from common.date import as_utc
from domain.llm.model.profile import ModelProfileVersion

_PASSTHROUGH_FIELDS = tuple(
    field.name
    for field in fields(ModelProfileVersion)
    if field.name
    not in {
        "protocol",
        "tokenizer_encoding",
        "created_at",
        "updated_at",
    }
)


def profile_to_domain(row: ModelProfileVersionModel) -> ModelProfileVersion:
    """构造不含凭据明文的模型版本投影。"""
    values = {name: getattr(row, name) for name in _PASSTHROUGH_FIELDS}
    return ModelProfileVersion(
        protocol="chat_completions",
        tokenizer_encoding=cast(Literal["cl100k_base", "o200k_base"], row.tokenizer_encoding),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        **values,
    )


__all__ = ["profile_to_domain"]
