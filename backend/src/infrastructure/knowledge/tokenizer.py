"""Deterministic Chinese search tokenization."""

from __future__ import annotations

import re


class JiebaChineseTokenizer:
    """Use search-mode Chinese terms and preserve identifiers and versions."""

    _identifier = re.compile(r"[A-Za-z][A-Za-z0-9_.:/+-]*|\d+(?:\.\d+)+")

    def tokenize(self, text: str) -> tuple[str, ...]:
        try:
            import jieba  # type: ignore[import-untyped]
        except ImportError as error:
            raise RuntimeError("knowledge_tokenizer_dependency_missing") from error
        words = (word.strip().lower() for word in jieba.cut_for_search(text))
        tokens = [word for word in words if word]
        tokens.extend(match.group(0).lower() for match in self._identifier.finditer(text))
        return tuple(dict.fromkeys(tokens))
