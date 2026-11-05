"""消息 ORM 与领域模型转换。"""

from adapter.message.converter.message import (
    conversation_to_domain,
    deserialize_metadata,
    deserialize_parts,
    execution_to_domain,
    serialize_metadata,
    serialize_parts,
)

__all__ = [
    "conversation_to_domain",
    "deserialize_metadata",
    "deserialize_parts",
    "execution_to_domain",
    "serialize_metadata",
    "serialize_parts",
]
