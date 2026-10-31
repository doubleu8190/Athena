"""Worker v1 原始 JSON 请求体的 Schema 校验入口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import JsonValue


_SCHEMA = json.loads(
    Path(__file__).with_name("worker_protocol_v1.schema.json").read_text(
        encoding="utf-8"
    )
)


def validate_worker_request(schema_name: str, body: dict[str, JsonValue]) -> None:
    """按应用包内冻结的 Worker v1 Schema 校验原始 JSON 对象。

    参数：
    - `schema_name`：协议 Schema `$defs` 中的请求名称。
    - `body`：尚未经过 Pydantic 归一化的 JSON 对象。

    返回：通过校验时返回 `None`。

    异常：`jsonschema.ValidationError` 表示 body 不符合 Worker v1 协议。
    """
    schema = cast(dict[str, JsonValue], _SCHEMA)
    validator = Draft202012Validator(
        {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{schema_name}",
        },
        format_checker=FormatChecker(),
    )
    validator.validate(body)


__all__ = ["validate_worker_request"]
