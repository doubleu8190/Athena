"""双人标注、仲裁、不可变发布和数据集导出。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athena.evaluation.models import Annotation, EvalCase, Locator, RetrievalItem
from athena.evaluation.storage import JsonlEventStore
from athena.utils.ids import generate_time_id


@dataclass(frozen=True)
class Label:
    """表示一次独立的检索标注结果。

    属性：
        task_id: 标注任务标识；发布后不可再修改。
        annotator_id: 标注人标识。
        relevance: 相关性等级，取值为 0 到 3。
        locator_correct: 定位信息是否正确；无法判断时为 ``None``。
        should_be_empty: 查询是否应返回空结果；无法判断时为 ``None``。
        error_stage: 问题发生阶段。
        created_at: ISO 8601 格式的创建时间。
    """

    task_id: str
    annotator_id: str
    relevance: int
    locator_correct: bool | None
    should_be_empty: bool | None
    error_stage: str
    created_at: str

    def __post_init__(self) -> None:
        """校验相关性等级和错误阶段，拒绝不可审计的标注值。

        异常：
            ValueError: ``relevance`` 不在 0 到 3，或 ``error_stage`` 不受支持。
        """
        if self.relevance not in (0, 1, 2, 3):
            raise ValueError("relevance must be 0, 1, 2, or 3")
        if self.error_stage not in {
            "parse",
            "index",
            "candidate",
            "fusion",
            "selection",
            "unknown",
        }:
            raise ValueError("invalid error_stage")


class AnnotationService:
    """管理双人标注、仲裁和不可变用例发布。"""

    def __init__(self, store: JsonlEventStore) -> None:
        """创建标注服务。

        参数：
            store: 追加写入标注事件的持久化存储。
        """
        self.store = store
        self._labels: dict[str, list[Label]] = {}
        self._released: set[str] = set()

    def add_label(self, label: Label) -> None:
        """添加一条标注并写入审计事件。

        参数：
            label: 已通过自身字段校验的标注。

        异常：
            ValueError: 该任务已经发布，不能继续修改。
        """
        if label.task_id in self._released:
            raise ValueError("released annotation cannot be modified")
        self._labels.setdefault(label.task_id, []).append(label)
        self.store.append({"type": "label", **asdict(label)})

    def arbitrate(self, task_id: str, *, arbitrator_id: str) -> dict[str, Any]:
        """合并至少两条独立标注，生成可追踪的仲裁结果。

        参数：
            task_id: 待仲裁的任务标识。
            arbitrator_id: 执行仲裁的人员标识。

        返回值：
            包含中位相关性、共识字段和版本号的仲裁字典。

        异常：
            ValueError: 任务不存在或独立标注少于两条。
        """
        labels = self._labels.get(task_id, [])
        if len(labels) < 2:
            raise ValueError("at least two independent labels are required")
        relevance = sorted(label.relevance for label in labels)
        result = {
            "type": "arbitration",
            "task_id": task_id,
            "arbitrator_id": arbitrator_id,
            "relevance": relevance[len(relevance) // 2],
            "locator_correct": (
                labels[0].locator_correct
                if all(
                    label.locator_correct == labels[0].locator_correct
                    for label in labels
                )
                else None
            ),
            "should_be_empty": (
                labels[0].should_be_empty
                if all(
                    label.should_be_empty == labels[0].should_be_empty
                    for label in labels
                )
                else None
            ),
            "error_stage": (
                labels[0].error_stage
                if all(label.error_stage == labels[0].error_stage for label in labels)
                else "unknown"
            ),
            "annotation_version": generate_time_id(),
        }
        self.store.append(result)
        return result

    def release_case(
        self,
        *,
        task_id: str,
        case_id: str,
        query: str,
        scope: dict[str, Any],
        document_alias: str,
        locator: dict[str, Any] | None,
        quote_sha256: str | None,
        arbitrated: dict[str, Any],
        labels: list[str],
    ) -> EvalCase:
        """将仲裁结果固化为不可变评估用例。

        参数：
            task_id: 原标注任务标识。
            case_id: 新评估用例标识。
            query: 用例查询文本。
            scope: 检索范围描述。
            document_alias: 相关文档的稳定别名。
            locator: 文档定位信息；无明确定位时为 ``None``。
            quote_sha256: 引用文本哈希；无引用时为 ``None``。
            arbitrated: ``arbitrate`` 生成的结果。
            labels: 用例标签列表。

        返回值：
            已包含仲裁元数据的 ``EvalCase``。

        异常：
            ValueError: 仲裁结果不属于指定任务。
        """
        if arbitrated.get("task_id") != task_id:
            raise ValueError("arbitration does not belong to task")
        self._released.add(task_id)
        annotation = Annotation(
            "arbitrated",
            (arbitrated["arbitrator_id"],),
            datetime.now(timezone.utc).isoformat(),
            annotation_version=arbitrated["annotation_version"],
        )
        relevant = (
            ()
            if arbitrated.get("should_be_empty")
            else (
                RetrievalItem(
                    document_alias,
                    Locator.from_dict(locator) if locator else None,
                    quote_sha256,
                    int(arbitrated["relevance"]),
                    "arbitrated feedback",
                ),
            )
        )
        return EvalCase(
            case_id,
            query,
            scope,
            relevant=relevant,
            expect_empty=bool(arbitrated.get("should_be_empty")),
            labels=tuple(labels),
            annotation=annotation,
        )

    def export_case(self, path: Path, case: EvalCase) -> None:
        """以 JSONL 形式追加导出一个评估用例。

        参数：
            path: 输出文件路径；父目录不存在时会自动创建。
            case: 要导出的评估用例。

        异常：
            OSError: 输出目录或文件无法创建或写入。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        value = asdict(case)
        with path.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    default=lambda item: item.value if hasattr(item, "value") else item,
                )
                + "\n"
            )
