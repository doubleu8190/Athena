"""Run 用例向 HTTP 边界返回的稳定结果。"""

from __future__ import annotations

from dataclasses import dataclass

from domain.run.model import Run


@dataclass(frozen=True, slots=True)
class RunListResult:
    """稳定游标分页结果。"""

    items: tuple[Run, ...]
    next_cursor: str | None


__all__ = ["RunListResult"]
