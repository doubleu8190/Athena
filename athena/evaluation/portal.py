"""个人助手检索评估门户的本地存储和领域操作。

该模块服务桌面端低频管理接口。它使用独立的 JSONL 文件保存原始检索记录、
用户反馈和已发布用例，不依赖生产 SQLite，也不改变核心检索流程。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from athena.evaluation.models import Annotation, EvalCase, Locator, RetrievalItem
from athena.utils.ids import generate_time_id


TARGET_FILES = {
    "records": "retrieval-records.jsonl",
    "feedback": "feedback.jsonl",
    "cases": "cases.jsonl",
    "reports": "reports",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> Any:
    """将 Pydantic、Enum 和 dataclass 值转换为 JSON 兼容数据。"""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "as_log_fields"):
        return value.as_log_fields()
    if hasattr(value, "value"):
        return value.value
    return value


class EvaluationPortal:
    """提供个人评估页面使用的持久化操作。"""

    def __init__(self, root: str | Path, *, record_enabled: bool = True, sample_rate: float = 1.0) -> None:
        self.root = Path(root)
        self.reports_dir = self.root / "reports"
        self._lock = threading.RLock()
        self.record_enabled = record_enabled
        self.sample_rate = sample_rate
        self.root.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    @property
    def data_directory(self) -> str:
        return str(self.root)

    def _path(self, target: str) -> Path:
        if target not in TARGET_FILES:
            raise ValueError(f"unknown evaluation target: {target}")
        return self.root / TARGET_FILES[target]

    def _read_jsonl(self, target: str) -> list[dict[str, Any]]:
        path = self._path(target)
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def _append(self, target: str, value: dict[str, Any]) -> None:
        path = self._path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")

    def record_retrieval(self, record: dict[str, Any]) -> None:
        """追加一条完整检索记录；写入失败由调用方隔离，不影响主回答。"""
        if not self.record_enabled:
            return
        # Sampling is deliberately applied at the write boundary so an explicit
        # feedback request can still enable recording before it reaches here.
        if self.sample_rate <= 0:
            return
        if self.sample_rate < 1.0:
            import random
            if random.random() > self.sample_rate:
                return
        value = {"type": "retrieval_record", "created_at": _now(), **record}
        if not value.get("event_id"):
            value["event_id"] = generate_time_id()
        self._append("records", value)

    def list_events(self, *, session_id: str | None = None, source: str | None = None, feedback_status: str | None = None) -> list[dict[str, Any]]:
        rows = self._latest_by(self._read_jsonl("records"), "event_id")
        feedback = self.feedback_by_event()
        result = []
        for row in rows:
            if session_id and row.get("session_id", row.get("scope", {}).get("session_id")) != session_id:
                continue
            if source and row.get("source", row.get("scope", {}).get("source")) != source:
                continue
            event_feedback = feedback.get(str(row.get("event_id")))
            if feedback_status and (event_feedback or {}).get("rating") != feedback_status:
                continue
            result.append(self._event_summary(row, event_feedback))
        return self._sort_recent(result)

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        rows = [row for row in self._read_jsonl("records") if row.get("event_id") == event_id]
        if not rows:
            return None
        row = rows[-1]
        return {**self._event_summary(row, self.feedback_by_event().get(event_id)),
                "scope": row.get("scope", {}),
                "results": row.get("results", []),
                "trace": row.get("trace"),
                "runtime": row.get("runtime", {})}

    def submit_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = str(payload.get("event_id", "")).strip()
        rating = str(payload.get("rating", "")).strip()
        if not event_id:
            raise ValueError("event_id is required")
        if rating not in {"accepted", "rejected", "corrected"}:
            raise ValueError("rating must be accepted, rejected, or corrected")
        event = self.get_event(event_id)
        if event is None:
            raise KeyError(f"retrieval event not found: {event_id}")
        correct_ids = [str(item) for item in payload.get("correct_result_ids", [])]
        expect_empty = payload.get("expect_empty")
        if rating == "corrected" and not correct_ids and expect_empty is not True:
            raise ValueError("corrected feedback requires correct_result_ids or expect_empty=true")
        if expect_empty is True and correct_ids:
            raise ValueError("expect_empty=true cannot include correct_result_ids")
        previous = self.feedback_by_event().get(event_id)
        feedback_id = previous.get("feedback_id") if previous else generate_time_id()
        feedback = {
            "type": "feedback",
            "feedback_id": feedback_id,
            "event_id": event_id,
            "rating": rating,
            "correct_result_ids": correct_ids,
            "expect_empty": expect_empty if rating == "corrected" else None,
            "gain": payload.get("gain") if rating == "corrected" else None,
            "comment": str(payload.get("comment", "")).strip() or None,
            "created_at": _now(),
        }
        if feedback["gain"] is not None and feedback["gain"] not in (1, 2, 3):
            raise ValueError("gain must be 1, 2, or 3")
        self._append("feedback", feedback)
        return self._feedback_view(feedback)

    def list_feedback(self, *, status: str | None = None, rating: str | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.feedback_by_event().values()
        events = {row["event_id"]: row for row in self._read_jsonl("records")}
        result = []
        for row in rows:
            if status and self._feedback_status(row) != status:
                continue
            if rating and row.get("rating") != rating:
                continue
            event = events.get(row.get("event_id"), {})
            scope = event.get("scope", {})
            if session_id and event.get("session_id", scope.get("session_id")) != session_id:
                continue
            result.append(self._feedback_view(row))
        return self._sort_recent(result)

    def promote(self, feedback_id: str, *, case_id: str | None = None, labels: list[str] | None = None) -> dict[str, Any]:
        feedback = next((row for row in self.feedback_by_event().values() if row.get("feedback_id") == feedback_id), None)
        if feedback is None:
            raise KeyError(f"feedback not found: {feedback_id}")
        if feedback.get("rating") != "corrected":
            raise ValueError("only corrected feedback can be promoted")
        if feedback.get("promoted_case_id"):
            existing = self.case_by_id(str(feedback["promoted_case_id"]))
            if existing:
                return {"case": existing, "dataset_version": self._manifest().get("version", "unknown")}
        event = self.get_event(str(feedback["event_id"]))
        if event is None:
            raise KeyError(f"retrieval event not found: {feedback['event_id']}")
        source = str(event.get("source") or event.get("scope", {}).get("source") or "memory")
        case_id = case_id or f"feedback-{feedback['event_id']}"
        case_labels = labels or ["user-corrected", source]
        valid_labels = {"memory", "file", "text", "pdf", "excel", "code", "image", "user-corrected", "expected_empty"}
        unknown = set(case_labels) - valid_labels
        if unknown:
            raise ValueError(f"unknown labels: {', '.join(sorted(unknown))}")
        annotation = Annotation("approved", ("owner",), _now(), source="user-feedback")
        relevant: list[RetrievalItem] = []
        result_map = {str(item.get("id")): item for item in event.get("results", []) if isinstance(item, dict)}
        gain = int(feedback.get("gain") or 3)
        rationale = feedback.get("comment") or "User confirmed this retrieval result."
        for result_id in feedback.get("correct_result_ids", []):
            result = result_map.get(str(result_id), {})
            alias = str(result.get("document_alias") or event.get("scope", {}).get("document_alias") or result_id)
            locator_value = result.get("locator")
            locator = Locator.from_dict(locator_value) if isinstance(locator_value, dict) and locator_value else None
            relevant.append(RetrievalItem(alias, locator=locator, gain=gain, rationale=rationale))
        expect_empty = feedback.get("expect_empty") is True
        if not expect_empty and not relevant:
            raise ValueError("corrected feedback has no promotable result")
        scope = {**dict(event.get("scope", {"source": source})), "event_id": event["event_id"]}
        case = EvalCase(case_id, str(event.get("query", "")), scope, relevant=tuple(relevant), expect_empty=expect_empty, labels=tuple(case_labels), annotation=annotation)
        self._append_case(case)
        updated_feedback = {**feedback, "promoted_case_id": case.case_id}
        self._append("feedback", updated_feedback)
        return {"case": self._case_summary(case), "dataset_version": self._manifest().get("version", "unknown")}

    def list_cases(self, *, label: str | None = None, source: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        result = []
        for case in self._read_cases():
            summary = self._case_summary(case)
            if label and label not in summary["labels"]:
                continue
            if source and summary["source"] != source:
                continue
            if status and summary["status"] != status:
                continue
            result.append(summary)
        return self._sort_recent(result)

    def list_reports(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.reports_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(report, dict):
                continue
            summary = self._report_summary(path, report)
            if kind and summary["kind"] != kind:
                continue
            result.append(summary)
        return result

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        path = self._report_path(report_id)
        if path is None or not path.exists():
            return None
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return {**self._report_summary(path, report), **report}

    def settings(self) -> dict[str, Any]:
        return {"record_enabled": self.record_enabled, "record_sample_rate": self.sample_rate, "data_directory": self.data_directory, "counts": self.counts()}

    def update_settings(self, *, record_enabled: bool | None = None, record_sample_rate: float | None = None) -> dict[str, Any]:
        if record_enabled is not None:
            self.record_enabled = record_enabled
        if record_sample_rate is not None:
            if not 0 <= record_sample_rate <= 1:
                raise ValueError("record_sample_rate must be between 0 and 1")
            self.sample_rate = record_sample_rate
        return self.settings()

    def counts(self) -> dict[str, int]:
        return {"records": len(self._latest_by(self._read_jsonl("records"), "event_id")), "feedback": len(self.feedback_by_event()), "cases": len(self._read_cases()), "reports": len(list(self.reports_dir.glob("*.json")))}

    def clear(self, targets: Iterable[str]) -> None:
        targets = list(targets)
        if not targets or any(target not in TARGET_FILES for target in targets):
            raise ValueError("targets must contain only records, feedback, cases, or reports")
        with self._lock:
            for target in targets:
                path = self._path(target)
                if target == "reports":
                    for report in self.reports_dir.glob("*"):
                        if report.is_file():
                            report.unlink()
                elif path.exists():
                    path.unlink()
            if "cases" in targets:
                manifest = self.root / "manifest.json"
                if manifest.exists():
                    manifest.unlink()

    def record_trace(self, trace: Any) -> None:
        """把现有 RetrievalTrace 转成可回顾的最小事件。"""
        try:
            fields = trace.as_log_fields()
            self.record_retrieval({
                "event_id": trace.request_id,
                "source": trace.source_scope,
                "query": trace.query,
                "scope": {"source": trace.source_scope},
                "results": [{"id": item_id, "source": trace.source_scope} for item_id in fields.get("selected_ids", [])],
                "trace": fields,
                "result_count": len(fields.get("selected_ids", [])),
                "total_duration_ms": fields.get("total_duration_ms"),
            })
        except Exception:
            return

    def _append_case(self, case: EvalCase) -> None:
        value = {
            "case_id": case.case_id, "query": case.query, "scope": case.scope,
            "relevant": [{"document_alias": item.document_alias, "locator": item.locator.values if item.locator else None, "quote_sha256": item.quote_sha256, "gain": item.gain, "rationale": item.rationale} for item in case.relevant],
            "must_not_return": [], "expect_empty": case.expect_empty, "labels": list(case.labels),
            "annotation": {"status": case.annotation.status, "annotator_ids": list(case.annotation.annotator_ids), "reviewed_at": case.annotation.reviewed_at, "source": case.annotation.source, "annotation_version": case.annotation.annotation_version} if case.annotation else None,
        }
        self._append("cases", value)
        self._write_manifest()

    def _read_cases(self) -> list[EvalCase]:
        path = self._path("cases")
        if not path.exists():
            return []
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
                annotation_value = value.get("annotation") or {}
                annotation = Annotation(str(annotation_value.get("status", "approved")), tuple(annotation_value.get("annotator_ids", ["owner"])), annotation_value.get("reviewed_at") or _now(), source=annotation_value.get("source"), annotation_version=str(annotation_value.get("annotation_version", "1")))
                relevant = tuple(RetrievalItem(str(item["document_alias"]), Locator.from_dict(item["locator"]) if item.get("locator") else None, item.get("quote_sha256"), int(item.get("gain", 1)), str(item.get("rationale", ""))) for item in value.get("relevant", []))
                result.append(EvalCase(str(value["case_id"]), str(value["query"]), dict(value["scope"]), relevant=relevant, expect_empty=bool(value.get("expect_empty")), labels=tuple(value.get("labels", [])), annotation=annotation))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return result

    def _write_manifest(self) -> None:
        case_path = self._path("cases")
        digest = hashlib.sha256(case_path.read_bytes() if case_path.exists() else b"").hexdigest()
        manifest = {"dataset_id": "athena-personal", "version": datetime.now(timezone.utc).strftime("v%Y%m%d%H%M%S"), "created_at": _now(), "redaction_policy": "local-personal", "case_count": len(self._read_cases()), "content_manifest_sha256": digest, "label_schema_version": "1", "min_annotators": 1}
        (self.root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _manifest(self) -> dict[str, Any]:
        path = self.root / "manifest.json"
        if not path.exists():
            return {"version": "empty"}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"version": "unknown"}
        except (OSError, json.JSONDecodeError):
            return {"version": "unknown"}

    @staticmethod
    def _latest_by(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.get(key):
                latest[str(row[key])] = row
        return list(latest.values())

    @staticmethod
    def _sort_recent(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda row: str(row.get("created_at", "")), reverse=True)

    def feedback_by_event(self) -> dict[str, dict[str, Any]]:
        return {str(row["event_id"]): row for row in self._read_jsonl("feedback") if row.get("event_id")}

    def case_by_id(self, case_id: str) -> dict[str, Any] | None:
        for case in self._read_cases():
            if case.case_id == case_id:
                return self._case_summary(case)
        return None

    def _event_summary(self, row: dict[str, Any], feedback: dict[str, Any] | None) -> dict[str, Any]:
        scope = row.get("scope", {})
        results = row.get("results", [])
        return {"event_id": row.get("event_id"), "session_id": row.get("session_id", scope.get("session_id", "")), "run_id": row.get("run_id"), "source": row.get("source", scope.get("source", "memory")), "query": row.get("query", ""), "created_at": row.get("created_at", ""), "result_count": row.get("result_count", len(results) if isinstance(results, list) else 0), "total_duration_ms": row.get("total_duration_ms"), "feedback": self._feedback_view(feedback) if feedback else None}

    @staticmethod
    def _feedback_status(row: dict[str, Any]) -> str:
        return "promoted" if row.get("promoted_case_id") else ("needs_correction" if row.get("rating") == "rejected" else str(row.get("rating", "")))

    @staticmethod
    def _feedback_view(row: dict[str, Any]) -> dict[str, Any]:
        return {"feedback_id": row.get("feedback_id"), "event_id": row.get("event_id"), "rating": row.get("rating"), "correct_result_ids": row.get("correct_result_ids", []), "expect_empty": row.get("expect_empty"), "gain": row.get("gain"), "comment": row.get("comment"), "created_at": row.get("created_at", ""), "promoted_case_id": row.get("promoted_case_id"), "can_promote": row.get("rating") == "corrected" and bool(row.get("expect_empty") or row.get("correct_result_ids")) and not row.get("promoted_case_id")}

    def _case_summary(self, case: EvalCase) -> dict[str, Any]:
        return {"case_id": case.case_id, "source_event_id": case.scope.get("event_id", ""), "source": case.scope.get("source", "memory"), "labels": list(case.labels), "dataset_version": self._manifest().get("version", "unknown"), "status": "active", "created_at": case.annotation.reviewed_at if case.annotation and case.annotation.reviewed_at else _now()}

    def _report_path(self, report_id: str) -> Path | None:
        if not report_id or Path(report_id).name != report_id or Path(report_id).suffix != ".json":
            report_id = f"{report_id}.json"
        if Path(report_id).name != report_id:
            return None
        path = self.reports_dir / report_id
        return path if path.parent == self.reports_dir else None

    def _report_summary(self, path: Path, report: dict[str, Any]) -> dict[str, Any]:
        metadata = report.get("metadata", {}) if isinstance(report.get("metadata"), dict) else {}
        kind = "comparison" if report.get("baseline") or report.get("candidate") or report.get("metrics") and report.get("baseline") else ("comparison" if "metrics" in report and "overall" not in report else "run")
        return {"report_id": path.stem, "kind": kind, "created_at": metadata.get("created_at") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(), "dataset_version": metadata.get("dataset_version") or report.get("dataset_version"), "snapshot": metadata.get("snapshot_id") or report.get("snapshot_id"), "model": metadata.get("llm_model") or metadata.get("model"), "app_version": metadata.get("application_commit")}
