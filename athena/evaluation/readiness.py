"""评估索引的门禁前检查。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from athena.evaluation.bindings import BindingIndex
from athena.evaluation.models import EvalCase


@dataclass
class ReadinessReport:
    """描述评估快照依赖的资源和绑定是否就绪。"""

    ready: bool
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def require_ready(self) -> None:
        """在报告未就绪时阻止评估继续执行。

        异常：
            RuntimeError: ``ready`` 为 ``False``。
        """
        if not self.ready:
            raise RuntimeError(
                "evaluation snapshot is not ready: " + "; ".join(self.failures)
            )


def check_bindings(
    cases: Iterable[EvalCase],
    bindings: BindingIndex,
    *,
    attachments_ready: bool = True,
    sqlite_ready: bool = True,
    chroma_ready: bool = True,
) -> ReadinessReport:
    """检查用例引用的资源绑定和后端就绪状态。

    参数：
        cases: 待执行的评估用例。
        bindings: 数据集别名绑定索引。
        attachments_ready: 附件解析和索引是否完成。
        sqlite_ready: SQLite 是否可用。
        chroma_ready: ChromaDB 是否可用。

    返回值：
        包含失败项、检查明细和缺失绑定的报告。
    """
    failures: list[str] = []
    checks = {
        "attachments_ready": attachments_ready,
        "sqlite_ready": sqlite_ready,
        "chroma_ready": chroma_ready,
    }
    for name, ok in checks.items():
        if not ok:
            failures.append(name)
    missing: list[str] = []
    for case in cases:
        for item in (*case.relevant, *case.must_not_return):
            locator_key = (
                item.locator.key(item.document_alias).split("#", 1)[1]
                if item.locator
                else None
            )
            if not bindings.resolve(item.document_alias, locator_key):
                missing.append(f"{case.case_id}:{item.document_alias}")
    if missing:
        failures.append("missing bindings: " + ", ".join(missing[:20]))
    checks["gold_locators_bound"] = not missing
    return ReadinessReport(
        not failures, checks, failures, {"missing_bindings": missing}
    )
