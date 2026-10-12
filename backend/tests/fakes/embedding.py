"""测试用 embedding 替身。"""

from __future__ import annotations

from collections.abc import Sequence


class _EchoEmbedder:
    """按文本长度生成确定性向量，供单元测试使用。"""

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    def __call__(self, values: Sequence[str]) -> list[list[float]]:
        """为每段文本生成等维度的测试向量。"""
        return [[float(len(value))] * self._dimension for value in values]


__all__ = ["_EchoEmbedder"]
