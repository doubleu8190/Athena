"""加载并验证版本化的检索评估数据集。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from athena.evaluation.models import (
    Annotation,
    DatasetManifest,
    EvalCase,
    Locator,
    RetrievalItem,
)


def _item(value: Any, *, name: str) -> RetrievalItem:
    """把 JSON 值校验并转换为检索标注项。

    异常：
        ValueError: ``value`` 不是对象或字段格式不正确。
    """
    if not isinstance(value, dict):
        raise ValueError(f"{name} item must be an object")
    return RetrievalItem(
        document_alias=str(value.get("document_alias", value.get("memory_alias", ""))),
        locator=(
            Locator.from_dict(value["locator"])
            if value.get("locator") is not None
            else None
        ),
        quote_sha256=value.get("quote_sha256"),
        gain=int(value.get("gain", 1)),
        rationale=str(value.get("rationale", "")),
    )


def _annotation(value: Any) -> Annotation | None:
    """把可选 JSON 标注转换为领域模型。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("annotation must be an object")
    return Annotation(
        status=str(value.get("status", "draft")),
        annotator_ids=tuple(value.get("annotator_ids", ())),
        reviewed_at=value.get("reviewed_at"),
        source=value.get("source"),
        annotation_version=str(value.get("annotation_version", "1")),
    )


def load_manifest(path: Path) -> DatasetManifest:
    """读取并校验评估数据集清单。

    参数：
        path: ``manifest.json`` 路径。

    返回值：
        已校验的 ``DatasetManifest``。

    异常：
        OSError: 文件无法读取。
        ValueError: 顶层 JSON 不是对象或清单字段无效。
        json.JSONDecodeError: 文件不是有效 JSON。
    """
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object")
    return DatasetManifest(**value)


def load_cases(
    path: Path, *, manifest: DatasetManifest | None = None
) -> list[EvalCase]:
    """逐行读取评估用例，并校验唯一性和清单数量。

    参数：
        path: ``cases.jsonl`` 路径。
        manifest: 可选清单；提供后会校验标注人数和用例总数。

    返回值：
        按文件顺序排列的评估用例列表。

    异常：
        OSError: 文件无法读取。
        ValueError: 行格式非法、用例 ID 重复、标注人数不足或文件为空。
    """
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            case = EvalCase(
                case_id=value["case_id"],
                query=value["query"],
                scope=dict(value["scope"]),
                relevant=tuple(
                    _item(item, name="relevant") for item in value.get("relevant", ())
                ),
                must_not_return=tuple(
                    _item(item, name="must_not_return")
                    for item in value.get("must_not_return", ())
                ),
                expect_empty=bool(value.get("expect_empty", False)),
                labels=tuple(value.get("labels", ())),
                annotation=_annotation(value.get("annotation")),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Invalid evaluation case at {path}:{line_number}: {exc}"
            ) from exc
        if case.case_id in seen:
            raise ValueError(
                f"Duplicate case_id at {path}:{line_number}: {case.case_id}"
            )
        if (
            manifest is not None
            and case.annotation is not None
            and len(case.annotation.annotator_ids) < manifest.min_annotators
        ):
            raise ValueError(
                f"{case.case_id} has fewer annotators than manifest.min_annotators"
            )
        seen.add(case.case_id)
        cases.append(case)
    if manifest is not None and len(cases) != manifest.case_count:
        raise ValueError(
            f"manifest case_count={manifest.case_count}, loaded {len(cases)} cases"
        )
    if not cases:
        raise ValueError(f"No evaluation cases found in {path}")
    return cases


def content_manifest_hash(root: Path) -> str:
    """按确定性顺序哈希数据集文件名和内容，排除清单文件。

    参数：
        root: 数据集根目录。

    返回值：
        用于版本追踪的 SHA-256 十六进制摘要。
    """
    digest = hashlib.sha256()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.name != "manifest.json"
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()
