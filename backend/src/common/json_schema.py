"""公共 JSON Schema 校验、外部引用防护与编译缓存工具。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator, validators  # type: ignore[import-untyped]
from referencing import Registry


def contains_external_ref(value: object) -> bool:
    """递归检查 JSON Schema 是否包含非文档内锚点（不以 # 开头）的外部引用。"""
    if isinstance(value, Mapping):
        ref = value.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            return True
        dynamic_ref = value.get("$dynamicRef")
        if isinstance(dynamic_ref, str) and not dynamic_ref.startswith("#"):
            return True
        return any(contains_external_ref(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(contains_external_ref(item) for item in value)
    return False


def ensure_object_root_schema(schema: object) -> dict[str, Any]:
    """校验输入为字典语义 Schema 且根类型为 object（省略 type 时补齐 object 副本）。"""
    if not isinstance(schema, Mapping):
        raise ValueError("schema_object_root_required")
    if schema.get("type", "object") != "object":
        raise ValueError("schema_object_root_required")
    if "type" in schema:
        return dict(schema)
    return {**schema, "type": "object"}


def check_json_schema(
    schema: Mapping[str, Any],
    *,
    allow_declared_draft: bool = False,
) -> None:
    """校验 JSON Schema 结构合法性；默认按 Draft 2020-12，可选按 $schema 声明解析。"""
    if not allow_declared_draft:
        Draft202012Validator.check_schema(schema)
        return
    declared_draft = schema.get("$schema")
    if declared_draft is not None and not isinstance(declared_draft, str):
        raise ValueError("schema_declared_draft_invalid")
    validator = (
        Draft202012Validator
        if declared_draft is None
        else validators.validator_for(schema, default=None)
    )
    if validator is None:
        raise ValueError("schema_declared_draft_unsupported")
    validator.check_schema(schema)


@lru_cache(maxsize=512)
def compile_draft202012_validator(
    schema_json: str,
    *,
    reject_external_ref: bool = True,
) -> Draft202012Validator:
    """按 JSON 字符串缓存校验器编译结果，统一使用空 Registry 隔离外部资源解析。"""
    schema = json.loads(schema_json)
    if reject_external_ref and contains_external_ref(schema):
        raise ValueError("tool_schema_external_ref")
    check_json_schema(schema)
    return Draft202012Validator(schema, registry=Registry())


def format_validation_path(error: Any) -> str:
    """将 jsonschema ValidationError 的绝对路径格式化为点分隔字段路径。"""
    return ".".join(str(item) for item in error.absolute_path)


__all__ = [
    "check_json_schema",
    "compile_draft202012_validator",
    "contains_external_ref",
    "ensure_object_root_schema",
    "format_validation_path",
]
