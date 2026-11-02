import json

from infrastructure.persistence.postgres.repositories.worker_orchestration_repository import (
    _worker_result_summary,
)


def test_worker_result_projection_keeps_single_text_newlines() -> None:
    content = "第一段\n第二段"

    assert _worker_result_summary({"content": content}) == content


def test_worker_result_projection_keeps_all_complex_fields_readable() -> None:
    output = {"summary": "通过", "content": "完整审核意见\n第二行", "score": 0.98}

    projected = _worker_result_summary(output)

    assert "\n  \"summary\"" in projected
    assert json.loads(projected) == output
