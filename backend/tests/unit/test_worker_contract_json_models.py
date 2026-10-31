"""Worker 稳定能力契约 JSON 模型的序列化验证。"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from application.orchestration.worker_contracts import WorkerCapabilityContract
from application.orchestration.worker_protocol_models import (
    WorkerCancelCommandModel,
    WorkerCancelReceiptModel,
    WorkerErrorModel,
    WorkerRegistrationResponseModel,
    WorkerResultReceiptModel,
    WorkerResultSubmissionModel,
    WorkerStopConfirmationModel,
    WorkerStopConfirmationReceiptModel,
)


_EXAMPLES = json.loads(
    (Path(__file__).parents[2] / "docs" / "worker-protocol-v1.examples.json").read_text()
)["examples"]
_PROTOCOL_SCHEMA = json.loads(
    (Path(__file__).parents[2] / "docs" / "worker-protocol-v1.schema.json").read_text()
)


def test_worker_capability_schemas_round_trip_aliases_and_open_fields() -> None:
    """JSON Schema 别名、扩展字段和 producer 定义结构往返后保持原样。"""
    source = {
        "can_do": ["读取任务输入并生成报告"],
        "cannot_do": ["不访问外部文件"],
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"record": {"type": "object", "properties": {"id": {"type": "string"}}}},
            "required": ["record"],
            "additionalProperties": False,
            "x-worker-hint": {"mode": "strict"},
        },
        "output_schema": {
            "type": "object",
            "properties": {"report": {"type": "string"}},
            "required": ["report"],
            "additionalProperties": False,
        },
    }

    contract = WorkerCapabilityContract.model_validate(source)
    dumped = contract.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert dumped["input_schema"]["$schema"] == source["input_schema"]["$schema"]
    assert dumped["input_schema"]["additionalProperties"] is False
    assert dumped["input_schema"]["x-worker-hint"] == {"mode": "strict"}
    assert dumped["input_schema"]["properties"] == source["input_schema"]["properties"]
    Draft202012Validator.check_schema(dumped["input_schema"])
    validator = Draft202012Validator(
        dumped["input_schema"], format_checker=FormatChecker()
    )
    assert validator.is_valid({"record": {"id": "abc"}})
    assert not validator.is_valid({"record": {"id": "abc"}, "extra": True})


def test_worker_capability_rejects_duplicate_schema_required_fields() -> None:
    """重复 required 字段应在应用模型边界拒绝。"""
    with pytest.raises(ValueError, match="worker_schema_required_fields_duplicate"):
        WorkerCapabilityContract.model_validate(
            {
                "can_do": ["执行"],
                "cannot_do": ["不越权"],
                "input_schema": {
                    "type": "object",
                    "required": ["item", "item"],
                },
                "output_schema": {"type": "object"},
            }
        )


@pytest.mark.parametrize(
    ("example_name", "model", "schema_name"),
    [
        ("registration_response", WorkerRegistrationResponseModel, "RegistrationResponse"),
        ("cancel_command", WorkerCancelCommandModel, "CancelCommand"),
        ("cancel_receipt_pending", WorkerCancelReceiptModel, "CancelReceipt"),
        ("cancel_timeout_receipt", WorkerCancelReceiptModel, "CancelReceipt"),
        ("stop_confirmation_timeout", WorkerStopConfirmationModel, "StopConfirmation"),
        ("stop_confirmation_timeout_receipt", WorkerStopConfirmationReceiptModel, "StopConfirmationReceipt"),
        ("stop_confirmation_task_cancel", WorkerStopConfirmationModel, "StopConfirmation"),
        ("stop_confirmation_task_cancel_receipt", WorkerStopConfirmationReceiptModel, "StopConfirmationReceipt"),
        ("result_success_duplicate_submission", WorkerResultSubmissionModel, "ResultSubmission"),
        ("result_failure", WorkerResultSubmissionModel, "ResultSubmission"),
        ("result_unknown_failure", WorkerResultSubmissionModel, "ResultSubmission"),
        ("result_success_duplicate_receipt", WorkerResultReceiptModel, "ResultReceipt"),
        ("result_failure_retry_receipt", WorkerResultReceiptModel, "ResultReceipt"),
        ("result_unknown_failure_final_receipt", WorkerResultReceiptModel, "ResultReceipt"),
    ],
)
def test_protocol_models_round_trip_s1_examples(
    example_name: str, model: type, schema_name: str
) -> None:
    """应用 Pydantic JSON 往返与已确认 v1 Schema 对相同样例作出一致裁定。"""
    example = _EXAMPLES[example_name]["payload"]
    validator = Draft202012Validator(
        {
            "$schema": _PROTOCOL_SCHEMA["$schema"],
            "$defs": _PROTOCOL_SCHEMA["$defs"],
            "$ref": f"#/$defs/{schema_name}",
        },
        format_checker=FormatChecker(),
    )
    assert validator.is_valid(example)
    parsed = model.model_validate(example)
    dumped = parsed.model_dump(mode="json", exclude_none=True)
    assert dumped == example


def test_protocol_models_reject_fields_not_in_s1_result_body() -> None:
    """执行身份来自 path，取消代次来自当前 Execution，不能进入结果正文。"""
    payload = dict(_EXAMPLES["result_success_duplicate_submission"]["payload"])
    payload["execution_id"] = "80000000-0000-4000-8000-000000000001"
    payload["cancellation_epoch"] = 0
    with pytest.raises(ValidationError):
        WorkerResultSubmissionModel.model_validate(payload)


def test_worker_error_code_length_matches_s1_schema() -> None:
    """错误码最多允许协议正则规定的 64 个 ASCII 字符。"""
    valid_code = "A" + "B" * 63
    WorkerErrorModel.model_validate(
        {"code": valid_code, "message": "failure", "retryable": True}
    )
    with pytest.raises(ValidationError):
        WorkerErrorModel.model_validate(
            {"code": valid_code + "B", "message": "failure", "retryable": True}
        )
