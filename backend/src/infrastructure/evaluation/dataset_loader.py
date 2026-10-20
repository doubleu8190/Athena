"""JSON and JSONL evaluation dataset loader."""

from __future__ import annotations

import json
import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

from domain.evaluation import (
    AgentExpectation,
    AnswerExpectation,
    DatasetManifest,
    EvalCase,
    RelevanceLabel,
    RetrievalExpectation,
)
from domain.evaluation.errors import DatasetValidationError

from .versioning import manifest_hash, version_id_for


def _read_records(path: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetValidationError(f"cannot read dataset: {path}") from exc
    if not text.strip():
        raise DatasetValidationError("dataset is empty")
    corpus: dict[str, str] = {}
    try:
        if path.suffix.lower() == ".jsonl":
            values = [json.loads(line) for line in text.splitlines() if line.strip()]
            metadata = next((item for item in values if isinstance(item, dict) and item.get("record_type") == "manifest"), {})
            corpus_value = metadata.get("corpus") or []
            corpus = {
                str(item["id"]): str(item["content"])
                for item in corpus_value
                if isinstance(item, dict) and item.get("id") is not None and item.get("content") is not None
            }
            records = [item for item in values if not (isinstance(item, dict) and item.get("record_type") == "manifest")]
        else:
            value = json.loads(text)
            if isinstance(value, dict) and isinstance(value.get("cases"), list):
                records = value["cases"]
                corpus_value = value.get("corpus") or []
                corpus = {
                    str(item["id"]): str(item["content"])
                    for item in corpus_value
                    if isinstance(item, dict) and item.get("id") is not None and item.get("content") is not None
                }
            elif isinstance(value, list):
                records = value
                corpus = {}
            else:
                records = [value]
                corpus = {}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DatasetValidationError(f"invalid JSON dataset: {path}") from exc
    if not all(isinstance(item, dict) for item in records):
        raise DatasetValidationError("every dataset record must be an object")
    return records, corpus


def _tuple_strings(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise DatasetValidationError(f"{field} must be a list of non-empty strings")
    return tuple(value)


def _parse_case(item: dict[str, Any]) -> EvalCase:
    case_id = item.get("case_id", item.get("id"))
    if case_id is None and isinstance(item.get("query"), str) and item["query"]:
        digest = hashlib.sha1(item["query"].encode("utf-8")).hexdigest()[:12]
        case_id = f"legacy-{digest}"
    if not isinstance(case_id, str) or not case_id:
        raise DatasetValidationError("case_id must be a non-empty string")
    task_type = str(item.get("task_type", "retrieval"))
    input_value = item.get("input")
    if input_value is None:
        input_value = {"query": item.get("query", "")}
    if not isinstance(input_value, dict):
        raise DatasetValidationError(f"{case_id}: input must be an object")

    retrieval_value = item.get("retrieval")
    if retrieval_value is None and "relevant_source_ids" in item:
        retrieval_value = {"relevant_source_ids": item["relevant_source_ids"]}
    retrieval = None
    if retrieval_value is not None:
        if not isinstance(retrieval_value, dict):
            raise DatasetValidationError(f"{case_id}: retrieval must be an object")
        sources = retrieval_value.get("relevant_sources")
        if sources is None:
            ids = retrieval_value.get("relevant_source_ids", ())
            if not isinstance(ids, list) or not all(isinstance(source_id, str) and source_id for source_id in ids):
                raise DatasetValidationError(f"{case_id}: relevant_source_ids must be a list of strings")
            labels = tuple(RelevanceLabel(source_id) for source_id in ids)
        else:
            if not isinstance(sources, list):
                raise DatasetValidationError(f"{case_id}: relevant_sources must be a list")
            labels = tuple(
                RelevanceLabel(str(source["source_id"]), int(source.get("grade", 2)))
                if isinstance(source, dict) and "source_id" in source
                else (_raise(f"{case_id}: every relevant source needs source_id"))
                for source in sources
            )
        if not labels:
            raise DatasetValidationError(f"{case_id}: retrieval needs at least one relevant source")
        retrieval = RetrievalExpectation(str(retrieval_value.get("scope", "knowledge")), labels)

    answer_value = item.get("answer")
    answer = None
    if answer_value is not None:
        if not isinstance(answer_value, dict):
            raise DatasetValidationError(f"{case_id}: answer must be an object")
        answer = AnswerExpectation(
            reference=answer_value.get("reference"),
            must_include=_tuple_strings(answer_value.get("must_include"), f"{case_id}.answer.must_include"),
            must_not_include=_tuple_strings(answer_value.get("must_not_include"), f"{case_id}.answer.must_not_include"),
            json_schema=answer_value.get("json_schema"),
        )

    agent_value = item.get("agent")
    agent = None
    if agent_value is not None:
        if not isinstance(agent_value, dict):
            raise DatasetValidationError(f"{case_id}: agent must be an object")
        agent = AgentExpectation(
            allowed_tools=_tuple_strings(agent_value.get("allowed_tools"), f"{case_id}.agent.allowed_tools"),
            required_tools=_tuple_strings(agent_value.get("required_tools"), f"{case_id}.agent.required_tools"),
            forbidden_tools=_tuple_strings(agent_value.get("forbidden_tools"), f"{case_id}.agent.forbidden_tools"),
            expected_effects=dict(agent_value.get("expected_effects") or {}),
        )
        if set(agent.required_tools) & set(agent.forbidden_tools):
            raise DatasetValidationError(f"{case_id}: required_tools conflicts with forbidden_tools")

    tags = _tuple_strings(item.get("tags"), f"{case_id}.tags")
    metadata = item.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise DatasetValidationError(f"{case_id}: metadata must be an object")
    return EvalCase(case_id, task_type, input_value, retrieval, answer, agent, tags, metadata)


def _raise(message: str) -> Any:
    raise DatasetValidationError(message)


def load_dataset(path: str | Path, *, dataset_name: str | None = None, schema_version: int = 1) -> DatasetManifest:
    source = Path(path)
    records, corpus = _read_records(source)
    cases = tuple(_parse_case(item) for item in records)
    case_ids = [case.case_id for case in cases]
    duplicates = sorted(case_id for case_id, count in Counter(case_ids).items() if count > 1)
    if duplicates:
        raise DatasetValidationError(f"duplicate case_id: {', '.join(duplicates)}")
    name = dataset_name or source.stem
    digest = manifest_hash(cases, corpus)
    return DatasetManifest(
        dataset_name=name,
        version_id=version_id_for(name, cases, corpus),
        manifest_sha256=digest,
        schema_version=schema_version,
        case_count=len(cases),
        tag_counts=dict(Counter(tag for case in cases for tag in case.tags)),
        cases=cases,
        corpus=corpus,
    )


__all__ = ["load_dataset"]
