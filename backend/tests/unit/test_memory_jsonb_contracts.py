"""记忆快照从 JSONB 读取时保留严格的领域契约。"""

import json

import pytest
from pydantic import ValidationError

from application.contracts.memory import (
    MemoryExecutionEvidenceSchema,
    MemoryFactExtractionSnapshotSchema,
)


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (MemoryFactExtractionSnapshotSchema, {"turn_id": "turn-1", "candidates": []}),
        (
            MemoryFactExtractionSnapshotSchema,
            {
                "turn_id": "turn-1",
                "candidates": [{
                    "content": "时区是 UTC", "source_turn_id": "turn-1",
                    "confidence": 0.9,
                    "fact": {
                        "scope_kind": "session", "scope_id": "session-1",
                        "subject_key": "user", "fact_key": "timezone",
                        "fact_value": "UTC", "cardinality": "single", "confidence": 0.9,
                        "asserted_at": "2026-01-01T00:00:00Z",
                        "source_refs": [{
                            "source_type": "turn", "source_id": "turn-1", "excerpt": "UTC",
                            "observed_at": "2026-01-01T00:00:00Z",
                        }],
                    },
                }],
            },
        ),
        (
            MemoryExecutionEvidenceSchema,
            {
                "run_id": "run-1",
                "source_fingerprint": "a" * 64,
                "total_task_count": 1,
                "truncated": False,
                "tasks": [{
                    "task_id": "task-1", "title": "任务", "status": "completed",
                    "execution_generation": 1,
                    "tool_outcomes": [{
                        "message_id": "message-1", "tool_name": "search",
                        "status": "success", "observed_at": "2026-01-01T00:00:00Z",
                    }],
                }],
                "source_refs": [{
                    "source_type": "run", "source_id": "run-1", "excerpt": "完成",
                    "observed_at": "2026-01-01T00:00:00Z",
                }],
            },
        ),
    ],
)
def test_jsonb_snapshot_round_trip(schema, payload):
    """JSON 数组在读回后仍能恢复为相同的不可变领域值。"""
    original = schema.model_validate_json(json.dumps(payload))
    stored = original.model_dump(mode="json")
    assert schema.model_validate(stored) == original


def test_jsonb_snapshot_rejects_non_array_candidates():
    """非数组候选不能绕过严格校验。"""
    with pytest.raises(ValidationError):
        MemoryFactExtractionSnapshotSchema.model_validate(
            {"turn_id": "turn-1", "candidates": "invalid"}
        )
