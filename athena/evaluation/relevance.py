"""将基于定位器的标签映射到运行时结果，不暴露内容。"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from athena.evaluation.models import EvalCase, RankedResult, RetrievalItem


def item_matches(item: RetrievalItem, result: RankedResult | Mapping[str, Any]) -> bool:
    """判断检索结果是否满足标注项的别名、定位和引用约束。"""
    actual_alias = (
        result.document_alias
        if isinstance(result, RankedResult)
        else result.get("document_alias")
    )
    if actual_alias is not None and actual_alias != item.document_alias:
        return False
    actual_locator = (
        result.locator
        if isinstance(result, RankedResult)
        else result.get("locator", {})
    )
    if item.locator is not None and not item.locator.matches(actual_locator):
        return False
    if item.quote_sha256 is not None:
        actual_hash = (
            result.get("quote_sha256") if isinstance(result, Mapping) else None
        )
        if actual_hash != item.quote_sha256:
            return False
    # 记忆级或文档级标签可能有意省略定位器；稳定别名仍足以匹配整个来源文档。
    if item.locator is None and item.quote_sha256 is None:
        return actual_alias == item.document_alias
    return True


def matched_items(
    case: EvalCase, results: Iterable[RankedResult | Mapping[str, Any]]
) -> set[int]:
    """返回已被结果覆盖的相关项下标集合。"""
    return {
        index
        for index, item in enumerate(case.relevant)
        if any(item_matches(item, result) for result in results)
    }


def prohibited_hit(
    case: EvalCase, results: Iterable[RankedResult | Mapping[str, Any]]
) -> bool:
    """判断结果是否命中任一禁止返回项。"""
    return any(
        item_matches(item, result)
        for item in case.must_not_return
        for result in results
    )
