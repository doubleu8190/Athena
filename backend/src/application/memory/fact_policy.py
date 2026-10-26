"""原子事实的保守基数规则。"""

from __future__ import annotations

from enum import StrEnum


class FactCardinality(StrEnum):
    """事实属性可容纳的值数量。"""

    SINGLE = "single"
    SET = "set"
    UNKNOWN = "unknown"


_SINGLE_VALUE_KEYS = frozenset(
    {
        "favorite_fruit",
        "preferred_language",
        "primary_language",
        "primary_role",
        "timezone",
        "release_date",
    }
)
_SET_VALUE_KEYS = frozenset(
    {
        "liked_fruit",
        "favorite_fruits",
        "programming_languages",
        "spoken_languages",
        "skills",
        "favorite_tools",
    }
)


def cardinality_for(fact_key: str) -> FactCardinality:
    """返回有明确规则的事实基数；未知属性不按单值覆盖。"""
    if fact_key in _SINGLE_VALUE_KEYS:
        return FactCardinality.SINGLE
    if fact_key in _SET_VALUE_KEYS:
        return FactCardinality.SET
    return FactCardinality.UNKNOWN


__all__ = ["FactCardinality", "cardinality_for"]
