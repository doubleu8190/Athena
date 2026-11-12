"""Run 固定 Skill 调用的纯校验与安全展示辅助。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from domain.skill import SkillParameterSummaryItem, SkillScriptDefinition

MAX_PARAMETER_SUMMARY_VALUE_CHARACTERS = 200


def summarize_parameters(
    script: SkillScriptDefinition,
    parameters: Mapping[str, Any],
) -> tuple[tuple[SkillParameterSummaryItem, ...], int]:
    """只展示有界枚举、常量、数字和布尔值，隐藏自由文本与嵌套正文。"""
    properties = script.parameters.get("properties", {})
    if not isinstance(properties, Mapping):
        return (), len(parameters)
    shown: list[SkillParameterSummaryItem] = []
    for name, schema in properties.items():
        if name not in parameters or not isinstance(name, str) or not isinstance(schema, Mapping):
            continue
        value = parameters[name]
        if not _safe_summary_value(value, schema):
            continue
        label = schema.get("title")
        if not isinstance(label, str) or not label.strip():
            label = name
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        shown.append(
            SkillParameterSummaryItem(
                name=name,
                label=label[:160],
                value=rendered[:MAX_PARAMETER_SUMMARY_VALUE_CHARACTERS],
            )
        )
    return tuple(shown), max(0, len(parameters) - len(shown))


def _safe_summary_value(value: Any, schema: Mapping[str, Any]) -> bool:
    """自由字符串可能含凭据或路径，只有枚举/常量或标量数值可展示。"""
    if "const" in schema:
        return True
    enum = schema.get("enum")
    if isinstance(enum, list):
        return True
    return isinstance(value, (bool, int, float)) and not isinstance(value, str)


__all__ = ["MAX_PARAMETER_SUMMARY_VALUE_CHARACTERS", "summarize_parameters"]
