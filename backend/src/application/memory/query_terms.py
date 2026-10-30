"""将用户查询拆成受限词项供中文全文与字面回退检索。"""

from __future__ import annotations

import re


def extract_memory_query_terms(query: str) -> tuple[str, ...]:
    """提取最多 16 个英文词项和中文相邻字词项。

    参数：
    - `query`（str）：原始自然语言检索问题；约束：无额外运行时限制。

    返回：
    - tuple[str, ...]：有序去重的非空词项；无法切词时返回截断后的原查询。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    normalized = query.casefold()
    terms = [
        term[:80]
        for term in re.findall(r"[a-z0-9][a-z0-9_./:-]{1,}", normalized)
    ]
    for sequence in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(sequence) <= 2:
            terms.append(sequence)
        else:
            terms.extend(
                sequence[index : index + 2]
                for index in range(len(sequence) - 1)
            )
    unique = tuple(dict.fromkeys(term for term in terms if term.strip()))
    if len(unique) > 16:
        indexes = tuple(round(index * (len(unique) - 1) / 15) for index in range(16))
        unique = tuple(unique[index] for index in indexes)
    return unique or (normalized.strip()[:80],)


__all__ = ["extract_memory_query_terms"]
