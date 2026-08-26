"""检索评估使用的版本化、无内容模型。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class RelevanceLabel(StrEnum):
    """支持的来源和查询标签。"""

    MEMORY = "memory"
    FILE = "file"
    USER_CORRECTED = "user-corrected"
    TEXT = "text"
    PDF = "pdf"
    EXCEL = "excel"
    CODE = "code"
    IMAGE = "image"
    NATURAL_LANGUAGE = "natural_language"
    EXACT_IDENTIFIER = "exact_identifier"
    EXACT_VALUE = "exact_value"
    FAQ = "faq"
    ERROR_CODE = "error_code"
    LOOKUP = "lookup"
    MULTI_HOP = "multi_hop"
    COMPLEX = "complex"
    AGGREGATE = "aggregate"
    EXPECTED_EMPTY = "expected_empty"
    ADVERSARIAL = "adversarial"


_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PATH_KEYS = {"path", "symbol", "start_line", "end_line"}
_EXCEL_KEYS = {"sheet", "start_row", "end_row", "value"}


def _require_string(value: Any, name: str) -> str:
    """校验值为非空字符串并返回原值。

    异常：
        ValueError: 值不是非空字符串。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _validate_alias(value: str, name: str) -> str:
    """校验别名满足稳定、可移植的字符约束。

    异常：
        ValueError: 别名包含不支持的字符或长度超限。
    """
    if not _ALIAS_RE.fullmatch(value):
        raise ValueError(f"{name} is not a stable document alias")
    return value


@dataclass(frozen=True)
class Locator:
    """独立于生成分块 ID 的来源位置。"""

    values: dict[str, Any]

    def __post_init__(self) -> None:
        """校验定位器的类型、必需字段和边界顺序。

        异常：
            ValueError: 定位器为空、类型不支持或边界非法。
        """
        if not self.values:
            raise ValueError("locator cannot be empty")
        keys = set(self.values)
        if "page" in keys:
            if (
                keys != {"page"}
                or not isinstance(self.values["page"], int)
                or self.values["page"] < 1
            ):
                raise ValueError("PDF locator requires a positive integer page")
        elif "sheet" in keys or "start_row" in keys or "end_row" in keys:
            required = {"sheet", "start_row", "end_row"}
            if not required <= keys or not isinstance(self.values["sheet"], str):
                raise ValueError("Excel locator requires sheet, start_row and end_row")
            if not all(
                isinstance(self.values[k], int) and self.values[k] >= 1
                for k in required - {"sheet"}
            ):
                raise ValueError("Excel row bounds must be positive integers")
            if self.values["start_row"] > self.values["end_row"]:
                raise ValueError("Excel start_row cannot exceed end_row")
        elif "path" in keys:
            if not isinstance(self.values["path"], str) or not self.values["path"]:
                raise ValueError("code locator requires path")
            if not ({"path", "start_line", "end_line"} <= keys):
                raise ValueError("code locator requires path, start_line and end_line")
            if not all(
                isinstance(self.values[k], int) and self.values[k] >= 1
                for k in ("start_line", "end_line")
            ):
                raise ValueError("code line bounds must be positive integers")
            if self.values["start_line"] > self.values["end_line"]:
                raise ValueError("code start_line cannot exceed end_line")
        elif "memory_alias" not in keys:
            raise ValueError(
                "unsupported locator; use page, Excel rows, code lines, or memory_alias"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Locator":
        """从映射创建并校验定位器。

        参数：
            value: 页面、表格行、代码行或记忆别名定位字段。

        返回值：
            规范化后的定位器。

        异常：
            ValueError: ``value`` 不是映射或字段约束不满足。
        """
        if not isinstance(value, Mapping):
            raise ValueError("locator must be an object")
        return cls(dict(value))

    def matches(self, actual: Mapping[str, Any]) -> bool:
        """判断实际定位信息是否包含本定位器的全部字段和值。"""
        return all(actual.get(key) == value for key, value in self.values.items())

    def key(self, document_alias: str) -> str:
        """生成用于绑定索引的确定性定位键。"""
        parts = [f"{key}={self.values[key]}" for key in sorted(self.values)]
        return f"{document_alias}#" + "&".join(parts)


@dataclass(frozen=True)
class RetrievalItem:
    """一个由人工标记为相关或禁止的来源项。"""

    document_alias: str
    locator: Locator | None = None
    quote_sha256: str | None = None
    gain: int = 1
    rationale: str = ""

    def __post_init__(self) -> None:
        """校验文档别名、引用哈希和相关性增益等级。

        异常：
            ValueError: 任一字段不符合评估数据约束。
        """
        _validate_alias(self.document_alias, "document_alias")
        if self.quote_sha256 is not None and not _HASH_RE.fullmatch(self.quote_sha256):
            raise ValueError("quote_sha256 must be a lowercase SHA-256 hash")
        if self.gain not in (1, 2, 3):
            raise ValueError("gain must be 1, 2, or 3")


@dataclass(frozen=True)
class Annotation:
    """描述用例的标注状态、人员和版本信息。"""

    status: str
    annotator_ids: tuple[str, ...]
    reviewed_at: str | None = None
    source: str | None = None
    annotation_version: str = "1"

    def __post_init__(self) -> None:
        """校验状态、标注人和已审核标注的时间要求。

        异常：
            ValueError: 状态不支持、没有标注人或已审核标注缺少时间。
        """
        if self.status not in {"draft", "approved", "arbitrated"}:
            raise ValueError("annotation status must be draft, approved, or arbitrated")
        if not self.annotator_ids:
            raise ValueError("at least one annotator is required")
        if self.status in {"approved", "arbitrated"} and not self.reviewed_at:
            raise ValueError("reviewed_at is required for approved annotations")


@dataclass(frozen=True)
class EvalCase:
    """描述一个带相关性和负例约束的检索评估用例。"""

    case_id: str
    query: str
    scope: dict[str, Any]
    relevant: tuple[RetrievalItem, ...] = ()
    must_not_return: tuple[RetrievalItem, ...] = ()
    expect_empty: bool = False
    labels: tuple[str, ...] = ()
    annotation: Annotation | None = None

    def __post_init__(self) -> None:
        """校验用例 ID、范围、标签及负例审核约束。

        异常：
            ValueError: 用例字段非法、标签未知或负例未审核。
        """
        _require_string(self.case_id, "case_id")
        _require_string(self.query, "query")
        if not isinstance(self.scope, dict) or not self.scope.get("source"):
            raise ValueError("scope.source is required")
        allowed = {label.value for label in RelevanceLabel}
        unknown = set(self.labels) - allowed
        if unknown:
            raise ValueError(f"unknown labels: {', '.join(sorted(unknown))}")
        if self.expect_empty and self.relevant:
            raise ValueError("expect_empty cases cannot contain relevant items")
        for item in (*self.relevant, *self.must_not_return):
            if item.gain == 3 and not item.rationale.strip():
                raise ValueError("gain=3 items require a rationale")
        if self.expect_empty or self.must_not_return:
            if self.annotation is None or not self.annotation.reviewed_at:
                raise ValueError("negative cases require reviewed annotation")


@dataclass(frozen=True)
class DatasetManifest:
    """描述评估数据集的版本、内容摘要和标注要求。"""

    dataset_id: str
    version: str
    created_at: str
    redaction_policy: str
    case_count: int
    content_manifest_sha256: str
    label_schema_version: str
    min_annotators: int = 1

    def __post_init__(self) -> None:
        """校验版本字段、数量边界和内容摘要格式。

        异常：
            ValueError: 字段为空、数量越界或摘要不是 SHA-256。
        """
        for name in (
            "dataset_id",
            "version",
            "created_at",
            "redaction_policy",
            "label_schema_version",
        ):
            _require_string(getattr(self, name), name)
        if self.case_count < 0 or self.min_annotators < 1:
            raise ValueError(
                "case_count must be non-negative and min_annotators positive"
            )
        if not _HASH_RE.fullmatch(self.content_manifest_sha256):
            raise ValueError("content_manifest_sha256 must be a lowercase SHA-256 hash")


@dataclass(frozen=True)
class RankedResult:
    """表示检索阶段返回的一个带排名结果。"""

    item_id: str
    locator: dict[str, Any] = field(default_factory=dict)
    document_alias: str | None = None
    route: str | None = None
    rank: int = 0
    score: float | None = None


@dataclass(frozen=True)
class RetrievalContext:
    """评估层的请求上下文，不进入生产检索方法。"""

    evaluation_run_id: str
    evaluation_case_id: str


@dataclass(frozen=True)
class RetrievalOutcome:
    """表示一个评估用例的候选集、最终集、耗时和错误信息。"""

    case_id: str
    candidates: tuple[RankedResult, ...] = ()
    final_results: tuple[RankedResult, ...] = ()
    stage_durations_ms: dict[str, float] = field(default_factory=dict)
    total_duration_ms: float = 0.0
    error_code: str | None = None
    trace: dict[str, Any] = field(default_factory=dict)
    retrieval_context: RetrievalContext | None = None
    llm_calls: int = 0
    llm_tokens: int = 0
