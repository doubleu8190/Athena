from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from infrastructure.persistence.postgres.repositories.task_execution_results import (
    load_task_execution_outputs,
    strict_result_json,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    _result_from_row,
)


class Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._rows


class Session:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def execute(self, _statement):
        return Result(self.rows)


@pytest.mark.asyncio
async def test_load_task_execution_outputs_resolves_large_and_inline_results():
    large_output = {"content": "x" * 70_000, "evidence": [{"source": "a"}]}
    serialized = strict_result_json(large_output).encode("utf-8")
    large_execution = SimpleNamespace(
        execution_id="execution-large",
        result_ref="task-result:execution-large",
        result_json={"result_ref": "task-result:execution-large"},
    )
    inline_execution = SimpleNamespace(
        execution_id="execution-inline",
        result_ref=None,
        result_json={"output": {"ok": True}},
    )
    artifact = SimpleNamespace(
        result_ref="task-result:execution-large",
        execution_id="execution-large",
        result_json=large_output,
        content_sha256=hashlib.sha256(serialized).hexdigest(),
        content_bytes=len(serialized),
    )

    outputs = await load_task_execution_outputs(
        Session([artifact]), [large_execution, inline_execution]
    )

    assert outputs == {
        "execution-large": large_output,
        "execution-inline": {"ok": True},
    }


@pytest.mark.asyncio
async def test_load_task_execution_outputs_rejects_integrity_mismatch():
    output = {"content": "complete"}
    artifact = SimpleNamespace(
        result_ref="task-result:execution-1",
        execution_id="execution-1",
        result_json=output,
        content_sha256="wrong-hash",
        content_bytes=len(json.dumps(output).encode("utf-8")),
    )
    execution = SimpleNamespace(
        execution_id="execution-1",
        result_ref="task-result:execution-1",
        result_json=None,
    )

    with pytest.raises(RuntimeError, match="integrity_failure"):
        await load_task_execution_outputs(Session([artifact]), [execution])


def test_worker_upstream_result_preserves_evidence_from_resolved_output():
    row = SimpleNamespace(
        task_id="task-1",
        status="completed",
        output_json='{"result_ref":"task-result:execution-1"}',
        error_json=None,
    )
    resolved_output = {
        "output": {"answer": "42"},
        "evidence": [{"source": "report.md"}],
        "uncertainties": ["source date unknown"],
        "retryable": False,
    }

    result = _result_from_row(row, resolved_output)

    assert result.output == {"answer": "42"}
    assert result.evidence == ({"source": "report.md"},)
    assert result.uncertainties == ("source date unknown",)


def test_worker_upstream_result_fails_if_projection_reference_is_unresolved():
    row = SimpleNamespace(
        task_id="task-1",
        status="completed",
        output_json=(
            '{"result_ref":"task-result:execution-1",'
            '"result_bytes":80000,"result_sha256":"sha256"}'
        ),
        error_json=None,
    )

    with pytest.raises(RuntimeError, match="reference_unavailable"):
        _result_from_row(row)


def test_strict_result_json_rejects_non_standard_nan():
    with pytest.raises(ValueError):
        strict_result_json({"score": float("nan")})
