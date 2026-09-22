"""在 PostgreSQL 上复现 SQLite FTS5 unicode61 与 bm25 的核心行为。"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[^\W]+", re.UNICODE)
_K1 = 1.2
_B = 0.75
_MIN_IDF = 1e-6


@dataclass(frozen=True)
class FTS5Document:
    """参与 FTS5 评分的文档。"""

    document_id: str
    content: str


def tokenize_unicode61(value: str) -> list[str]:
    """按项目原 SQLite unicode61 查询约定切分文本。

    参数：
        value: 待切分的文本。

    返回：
        连续的 ASCII 字母数字下划线或中文字符 token 列表。

    异常：
        不主动抛出业务异常。
    """
    # unicode61 默认折叠大小写并移除拉丁字母音标；这里保持相同的默认行为。
    return [
        "".join(
            char
            for char in unicodedata.normalize("NFKD", token.casefold())
            if not unicodedata.combining(char)
        )
        for token in _TOKEN_RE.findall(value)
    ]


def build_match_expression_terms(query: str) -> list[tuple[str, bool]]:
    """构造现有 FTS5 查询展开后的精确词和前缀词。

    参数：
        query: 用户输入的关键词。

    返回：
        ``(token, is_prefix)`` 列表；中文或长度至少为 3 的 token 同时保留
        精确和前缀匹配，短 ASCII token 只保留精确匹配。

    异常：
        不主动抛出业务异常。
    """
    terms: list[tuple[str, bool]] = []
    for token in tokenize_unicode61(query):
        terms.append((token, False))
        if re.search(r"[\u4e00-\u9fa5]", token) or len(token) >= 3:
            terms.append((token, True))
    return terms


def build_exact_terms(query: str) -> list[tuple[str, bool]]:
    """构造只包含精确 token 的 FTS5 查询项。"""
    return [(token, False) for token in tokenize_unicode61(query)]


def rank_bm25(
    documents: Sequence[FTS5Document], query: str, *, include_prefix: bool = True
) -> dict[str, float]:
    """计算 SQLite FTS5 默认 bm25 排名值（返回值为负数）。

    参数：
        documents: FTS 索引中的候选文档；文档长度和文档频率均从该集合计算。
        query: 已按 FTS5 约定展开的用户查询文本。

    返回：
        命中文档到 bm25 值的映射，值越小表示越相关；未命中为空映射。

    异常：
        不主动抛出业务异常。
    """
    terms = (
        build_match_expression_terms(query)
        if include_prefix
        else build_exact_terms(query)
    )
    if not terms or not documents:
        return {}

    tokenized = {
        document.document_id: tokenize_unicode61(document.content)
        for document in documents
    }
    lengths = {document_id: len(tokens) for document_id, tokens in tokenized.items()}
    average_length = sum(lengths.values()) / len(lengths)
    document_count = len(documents)
    scores: defaultdict[str, float] = defaultdict(float)

    # 每个 OR 子句独立计算一次 FTS5 单列 BM25，再把命中子句的贡献相加。
    # 这保留了“精确词 + 前缀词”同时命中时精确匹配更靠前的排序特征。
    for term, is_prefix in terms:
        frequencies: dict[str, int] = {}
        for document_id, document_tokens in tokenized.items():
            frequencies[document_id] = sum(
                token.startswith(term) if is_prefix else token == term
                for token in document_tokens
            )
        matching = {document_id: count for document_id, count in frequencies.items() if count}
        if not matching:
            continue
        document_frequency = len(matching)
        idf = math.log(
            (document_count - document_frequency + 0.5)
            / (document_frequency + 0.5)
        )
        # FTS5 对非正 IDF 使用极小正数，避免高频词反向惩罚命中文档。
        idf = max(idf, _MIN_IDF)
        for document_id, frequency in matching.items():
            length = lengths[document_id]
            denominator = frequency + _K1 * (
                1 - _B + _B * length / average_length
            )
            scores[document_id] -= idf * (frequency * (_K1 + 1) / denominator)
    return dict(scores)


def sorted_matches(
    documents: Iterable[FTS5Document], query: str
) -> list[tuple[FTS5Document, float]]:
    """返回按 SQLite FTS5 bm25 升序排列的命中文档。"""
    document_list = list(documents)
    scores = rank_bm25(document_list, query)
    return sorted(
        (
            (document, scores[document.document_id])
            for document in document_list
            if document.document_id in scores
        ),
        key=lambda item: item[1],
    )
