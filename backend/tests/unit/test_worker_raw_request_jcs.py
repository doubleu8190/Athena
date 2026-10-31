"""Worker 应用边界在协议解析前计算 JCS 请求摘要。"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import rfc8785
import pytest
from jsonschema import ValidationError

from application.orchestration.worker_registration import WorkerRegistrationService
from application.orchestration.worker_result_submission import (
    WorkerResultSubmissionService,
)
from application.orchestration.worker_stop_confirmation import (
    WorkerStopConfirmationService,
)
from application.orchestration.worker_protocol_validation import (
    validate_worker_request,
)


_EXAMPLES = json.loads(
    (Path(__file__).parents[2] / "docs" / "worker-protocol-v1.examples.json").read_text()
)["examples"]


class _Repository:
    def __init__(self) -> None:
        self.requests = []

    async def register_worker(self, intent):
        self.requests.append(intent)
        from domain.orchestration.worker_entities import WorkerRegistrationReceipt

        return WorkerRegistrationReceipt(
            instance_id="instance",
            instance_key=intent.instance_key,
            contract_id="contract",
            contract_revision=1,
            registered_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
        )

    async def record_result(self, *, submission):
        self.requests.append(submission)
        from domain.orchestration.worker_entities import WorkerResultReceipt

        return WorkerResultReceipt(
            receipt_id="receipt",
            execution_id=submission.execution_id,
            result_id=submission.result_id,
            disposition="recorded",
            reason="success_recorded",
            generation=submission.generation,
            received_at=datetime.now(timezone.utc),
        )

    async def confirm_stopped(self, confirmation):
        self.requests.append(confirmation)
        from domain.orchestration.worker_entities import WorkerStopReceipt

        return WorkerStopReceipt(
            stop_confirmation_id=confirmation.stop_confirmation_id,
            cancel_command_id=confirmation.cancel_command_id,
            execution_id=confirmation.execution_id,
            generation=confirmation.generation,
            cancellation_epoch=confirmation.cancellation_epoch,
            reason=confirmation.reason,
            disposition="capacity_released",
            received_at=datetime.now(timezone.utc),
        )


def _hash(body: dict[str, object]) -> str:
    return hashlib.sha256(rfc8785.dumps(body)).hexdigest()


@pytest.mark.asyncio
async def test_registration_hash_uses_original_json_before_uuid_and_defaults_normalize() -> None:
    repository = _Repository()
    service = WorkerRegistrationService(repository)
    source = dict(_EXAMPLES["registration_request"]["payload"])
    source["registration_request_id"] = "aa000000-0000-4000-8000-000000000001"
    changed = dict(source)
    changed["registration_request_id"] = source["registration_request_id"].upper()
    # Pydantic 会规范化 UUID 字符串，因此摘要必须基于原始请求对象计算。
    await service.register(source, service_principal="worker-service")
    await service.register(changed, service_principal="worker-service")
    assert repository.requests[0].registration_request_id == repository.requests[1].registration_request_id
    assert repository.requests[0].request_hash == _hash(source)
    assert repository.requests[1].request_hash == _hash(changed)
    assert repository.requests[0].request_hash != repository.requests[1].request_hash


@pytest.mark.asyncio
async def test_result_hash_preserves_uuid_spelling_from_original_json() -> None:
    repository = _Repository()
    service = WorkerResultSubmissionService(repository)
    source = dict(_EXAMPLES["result_success"]["payload"])
    changed = dict(source)
    changed["result_id"] = source["result_id"].upper()
    await service.submit(source, instance_id="instance", execution_id="execution")
    await service.submit(changed, instance_id="instance", execution_id="execution")
    assert repository.requests[0].result_id == repository.requests[1].result_id
    assert repository.requests[0].request_hash == _hash(source)
    assert repository.requests[1].request_hash == _hash(changed)
    assert repository.requests[0].request_hash != repository.requests[1].request_hash


@pytest.mark.asyncio
async def test_stop_hash_preserves_original_timestamp_offset() -> None:
    repository = _Repository()
    service = WorkerStopConfirmationService(repository)
    source = dict(_EXAMPLES["stop_confirmation_timeout"]["payload"])
    changed = dict(source)
    changed["stopped_at"] = source["stopped_at"].replace("Z", "+00:00")
    await service.confirm(source, instance_id="instance")
    await service.confirm(changed, instance_id="instance")
    assert repository.requests[0].stopped_at == repository.requests[1].stopped_at
    assert repository.requests[0].request_hash == _hash(source)
    assert repository.requests[1].request_hash == _hash(changed)
    assert repository.requests[0].request_hash != repository.requests[1].request_hash


def test_runtime_protocol_schema_matches_the_frozen_documentation_schema() -> None:
    """运行时请求校验资源与冻结的 S1 Schema 完全一致。"""
    runtime_schema = json.loads(
        (Path(__file__).parents[2] / "src/application/orchestration/worker_protocol_v1.schema.json").read_text()
    )
    assert runtime_schema == json.loads(
        (Path(__file__).parents[2] / "docs/worker-protocol-v1.schema.json").read_text()
    )
    packaged_schema = files("backend").joinpath(
        "src/application/orchestration/worker_protocol_v1.schema.json"
    )
    assert json.loads(packaged_schema.read_text(encoding="utf-8")) == runtime_schema


@pytest.mark.parametrize(
    ("schema_name", "example_name", "mutate"),
    [
        (
            "RegistrationRequest",
            "registration_request",
            lambda body: body.__setitem__("execution_timeout_seconds", "60"),
        ),
        (
            "RegistrationRequest",
            "registration_request",
            lambda body: body["capability"].__setitem__("can_do", "summarize"),
        ),
        (
            "ResultSubmission",
            "result_success",
            lambda body: body.__setitem__("generation", "1"),
        ),
        (
            "ResultSubmission",
            "result_success",
            lambda body: body.__setitem__("error", None),
        ),
        (
            "StopConfirmation",
            "stop_confirmation_timeout",
            lambda body: body.__setitem__("stopped", 1),
        ),
    ],
)
def test_raw_protocol_entry_rejects_json_values_outside_s1_schema(
    schema_name: str, example_name: str, mutate
) -> None:
    """原始 JSON 的类型、required 和跨字段约束由 S1 Schema 严格裁定。"""
    body = deepcopy(_EXAMPLES[example_name]["payload"])
    mutate(body)
    with pytest.raises(ValidationError):
        validate_worker_request(schema_name, body)
