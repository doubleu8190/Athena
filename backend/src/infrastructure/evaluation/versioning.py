"""Stable dataset serialization and version identifiers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from domain.evaluation import EvalCase


def case_payload(case: EvalCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "task_type": case.task_type,
        "input": dict(case.input),
        "retrieval": None if case.retrieval is None else {
            "scope": case.retrieval.scope,
            "relevant_sources": [
                {"source_id": item.source_id, "grade": item.grade}
                for item in case.retrieval.relevant_sources
            ],
        },
        "answer": None if case.answer is None else {
            "reference": case.answer.reference,
            "must_include": list(case.answer.must_include),
            "must_not_include": list(case.answer.must_not_include),
            "json_schema": case.answer.json_schema,
        },
        "agent": None if case.agent is None else {
            "allowed_tools": list(case.agent.allowed_tools),
            "required_tools": list(case.agent.required_tools),
            "forbidden_tools": list(case.agent.forbidden_tools),
            "expected_effects": dict(case.agent.expected_effects),
        },
        "tags": list(case.tags),
        "metadata": dict(case.metadata),
    }


def canonical_lines(cases: Iterable[EvalCase], corpus: Mapping[str, str] | None = None) -> bytes:
    values = [case_payload(case) for case in cases]
    values.sort(key=lambda item: item["case_id"])
    payload = b"".join(
        (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        for value in values
    )
    if corpus:
        payload += (json.dumps({"corpus": dict(sorted(corpus.items()))}, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return payload


def manifest_hash(cases: Iterable[EvalCase], corpus: Mapping[str, str] | None = None) -> str:
    return hashlib.sha256(canonical_lines(cases, corpus)).hexdigest()


def version_id_for(dataset_name: str, cases: Iterable[EvalCase], corpus: Mapping[str, str] | None = None) -> str:
    digest = manifest_hash(cases, corpus)
    return f"{dataset_name}@{digest[:12]}"


__all__ = ["canonical_lines", "case_payload", "manifest_hash", "version_id_for"]
