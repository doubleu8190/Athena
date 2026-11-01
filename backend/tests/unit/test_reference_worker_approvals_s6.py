"""参考 Worker S6 审批暂停、可靠申请和恢复结果回归。"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import rfc8785

from interfaces.http.worker_protocol import WorkerProtocolHttp
from reference_worker.config import ReferenceWorkerConfig
from reference_worker.runtime import ReferenceWorker


def _worker(tmp_path, *, count: int = 1) -> ReferenceWorker:
    """构造包含审批输入和输出约束的参考 Worker。"""
    capability = tmp_path / "capability.json"
    capability.write_text(
        json.dumps(
            {
                "can_do": ["Echo after approval"],
                "cannot_do": ["Execute real tools"],
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "approval_items": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "tool_name": {"type": "string"},
                                    "summary": {"type": "string"},
                                    "risk_level": {
                                        "enum": ["low", "medium", "high", "critical"]
                                    },
                                    "arguments": {"type": "object"},
                                },
                                "required": [
                                    "tool_name",
                                    "summary",
                                    "risk_level",
                                    "arguments",
                                ],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["value", "approval_items"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "echo": {"type": "object"},
                        "decisions": {"type": "array"},
                    },
                    "required": ["echo", "decisions"],
                    "additionalProperties": False,
                },
            }
        ),
        encoding="utf-8",
    )
    cert, key, ca = (tmp_path / name for name in ("worker.crt", "worker.key", "ca.crt"))
    for path in (cert, key, ca):
        path.touch()
    config = ReferenceWorkerConfig(
        orchestrator_url="https://athena.test",
        public_base_url="https://worker.test",
        service_token="worker-service-token",
        orchestrator_token="athena-orchestrator-token",
        instance_key=uuid4(),
        database_path=tmp_path / "worker.sqlite3",
        capability_path=capability,
        tls_certificate_path=cert,
        tls_private_key_path=key,
        ca_certificate_path=ca,
        result_retry_initial_seconds=0.1,
        result_retry_max_seconds=0.1,
        result_retry_jitter_ratio=0,
    )
    worker = ReferenceWorker(config)
    worker.store.set_setting(
        "registration",
        {
            "response": {
                "worker_instance_key": str(config.instance_key),
                "contract_id": "approval-contract",
                "contract_revision": 1,
            }
        },
    )
    worker._registered = True
    worker._accepting_tasks = True
    return worker


def _command(*, input_value: dict[str, object]) -> dict[str, object]:
    """生成引用审批能力的 TaskCommand。"""
    return {
        "protocol_version": "1.0",
        "command_id": str(uuid4()),
        "run_id": str(uuid4()),
        "plan_id": str(uuid4()),
        "task_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "generation": 1,
        "cancellation_epoch": 0,
        "operation_id": str(uuid4()),
        "contract_id": "approval-contract",
        "contract_revision": 1,
        "objective": "审批后回显",
        "acceptance_criteria": "输出决定集合",
        "input": input_value,
        "execution_grant": {
            "token": "opaque-test-execution-grant-token",
            "expires_at": "2026-10-31T08:30:00Z",
        },
    }


@pytest.mark.asyncio
async def test_approval_items_pause_and_resume_echo_is_idempotent(tmp_path) -> None:
    """审批申请先持久暂停，Resume 后只执行一次纯 echo。"""
    worker = _worker(tmp_path)
    command = _command(
        input_value={
            "value": "hello",
            "approval_items": [
                {
                    "tool_name": "demo.tool",
                    "summary": "需要审批",
                    "risk_level": "high",
                    "arguments": {"value": "x"},
                }
            ],
        }
    )
    await worker._accept_task(command, "task-hash")
    execution = worker.store.claim_execution()
    assert execution is not None
    await worker._execute(execution)

    approval_outbox = worker.store.claim_outbox(10**12)
    assert approval_outbox is not None
    assert approval_outbox["kind"] == "approval_request"
    approval_request = approval_outbox["body"]
    item = approval_request["items"][0]
    assert item["arguments_sha256"] == hashlib.sha256(
        rfc8785.dumps(item["arguments"])
    ).hexdigest()
    assert worker.store.pause_snapshot(
        pause_id=approval_request["pause_id"],
        execution_id=command["execution_id"],
    ) is not None

    receipt = {
        "protocol_version": "1.0",
        "approval_request_id": approval_request["approval_request_id"],
        "pause_id": approval_request["pause_id"],
        "disposition": "accepted",
        "status": "accepted",
        "expires_at": "2026-10-31T08:32:00Z",
        "items": [
            {
                "item_id": item["item_id"],
                "approval_id": str(uuid4()),
                "arguments_sha256": item["arguments_sha256"],
            }
        ],
        "received_at": "2026-10-31T08:30:00Z",
    }
    worker._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(202, json=receipt)
        )
    )
    await worker._submit_outbox(approval_outbox)
    assert worker.store.claim_outbox(10**12) is None

    decisions = [
        {
            "item_id": item["item_id"],
            "decision": "approved",
            "arguments_sha256": item["arguments_sha256"],
            "decided_at": "2026-10-31T08:31:00Z",
        }
    ]
    resume = {
        "protocol_version": "1.0",
        "command_id": str(uuid4()),
        "execution_id": command["execution_id"],
        "generation": 1,
        "cancellation_epoch": 0,
        "pause_id": approval_request["pause_id"],
        "approval_request_id": approval_request["approval_request_id"],
        "approval_id": receipt["items"][0]["approval_id"],
        "decisions": decisions,
        "decision_set_sha256": hashlib.sha256(rfc8785.dumps(decisions)).hexdigest(),
        "execution_grant": command["execution_grant"],
    }
    accepted, status = await worker._accept_resume(resume, "resume-hash")
    assert status == 202 and accepted.replayed is False
    duplicate, duplicate_status = await worker._accept_resume(resume, "resume-hash")
    assert duplicate_status == 200 and duplicate.replayed is True

    resumed_execution = worker.store.claim_execution()
    assert resumed_execution is not None
    assert resumed_execution["approval_decisions"] == decisions
    await worker._execute(resumed_execution)
    result = worker.store.claim_outbox(10**12)
    assert result is not None and result["kind"] == "result"
    assert result["body"]["output"] == {
        "echo": command["input"],
        "decisions": [{"item_id": item["item_id"], "decision": "approved"}],
    }
    assert worker.store.claim_execution() is None
    await worker.stop()


@pytest.mark.asyncio
async def test_approval_request_retry_reuses_same_body_and_resume_before_receipt(tmp_path) -> None:
    """申请回执丢失时同一 outbox 可重投，Resume 先到也不会重复暂停。"""
    worker = _worker(tmp_path)
    command = _command(
        input_value={
            "value": "early-resume",
            "approval_items": [
                {
                    "tool_name": "demo.tool",
                    "summary": "审批",
                    "risk_level": "medium",
                    "arguments": {},
                }
            ],
        }
    )
    await worker._accept_task(command, "task-hash")
    execution = worker.store.claim_execution()
    assert execution is not None
    await worker._execute(execution)
    first = worker.store.claim_outbox(10**12)
    assert first is not None and first["kind"] == "approval_request"
    request_body = first["body"]
    item = request_body["items"][0]
    decisions = [
        {
            "item_id": item["item_id"],
            "decision": "denied",
            "arguments_sha256": item["arguments_sha256"],
            "decided_at": "2026-10-31T08:31:00Z",
        }
    ]
    resume = {
        "protocol_version": "1.0",
        "command_id": str(uuid4()),
        "execution_id": command["execution_id"],
        "generation": 1,
        "cancellation_epoch": 0,
        "pause_id": request_body["pause_id"],
        "approval_request_id": request_body["approval_request_id"],
        "approval_id": str(uuid4()),
        "decisions": decisions,
        "decision_set_sha256": hashlib.sha256(rfc8785.dumps(decisions)).hexdigest(),
        "execution_grant": command["execution_grant"],
    }
    accepted, _ = await worker._accept_resume(resume, "resume-hash")
    assert accepted.replayed is False
    # Resume 已持久收件，但中心映射未知时仍保持暂停，防止未校验的授权生效。
    assert worker.store.pause_snapshot(
        pause_id=request_body["pause_id"], execution_id=command["execution_id"]
    ) is not None
    worker.store.retry_outbox(int(first["outbox_id"]), available_at=0, error="receipt_lost")
    replay = worker.store.claim_outbox(10**12)
    assert replay is not None
    assert replay["body"] == request_body
    receipt = {
        "protocol_version": "1.0",
        "approval_request_id": request_body["approval_request_id"],
        "pause_id": request_body["pause_id"],
        "approval_batch_id": None,
        "disposition": "accepted",
        "status": "accepted",
        "expires_at": "2026-10-31T08:32:00Z",
        "items": [
            {
                "item_id": item["item_id"],
                "approval_id": resume["approval_id"],
                "arguments_sha256": item["arguments_sha256"],
            }
        ],
        "received_at": "2026-10-31T08:30:00Z",
    }
    worker.store.record_approval_receipt(receipt)
    resumed_execution = worker.store.claim_execution()
    assert resumed_execution is not None
    assert resumed_execution["approval_decisions"] == decisions
    await worker.stop()


@pytest.mark.asyncio
async def test_cancel_rejects_pending_approval_outbox(tmp_path) -> None:
    """取消等待审批的 Execution 后不再投递审批申请。"""
    worker = _worker(tmp_path)
    command = _command(
        input_value={
            "value": "cancel",
            "approval_items": [
                {
                    "tool_name": "demo.tool",
                    "summary": "审批",
                    "risk_level": "low",
                    "arguments": {},
                }
            ],
        }
    )
    await worker._accept_task(command, "task-hash")
    execution = worker.store.claim_execution()
    assert execution is not None
    await worker._execute(execution)
    cancel = {
        "protocol_version": "1.0",
        "command_id": str(uuid4()),
        "execution_id": command["execution_id"],
        "generation": 1,
        "cancellation_epoch": 1,
        "reason": "task_cancelled",
    }
    await worker._accept_cancel(cancel, "cancel-hash")
    stop = worker.store.claim_outbox(10**12)
    assert stop is not None and stop["kind"] == "stop_confirmation"
    retry = worker.store.claim_outbox(10**12)
    assert retry is not None and retry["kind"] == "stop_confirmation"
    await worker.stop()


@pytest.mark.asyncio
async def test_resume_accepts_protocol_sorted_decisions_when_input_order_differs(tmp_path) -> None:
    """Resume 按 item_id 排序时不要求匹配暂停申请的原始输入顺序。"""
    worker = _worker(tmp_path)
    command = _command(
        input_value={
            "value": "ordered",
            "approval_items": [
                {
                    "tool_name": "demo.tool",
                    "summary": "审批",
                    "risk_level": "low",
                    "arguments": {},
                }
            ],
        }
    )
    await worker._accept_task(command, "task-hash")
    execution = worker.store.claim_execution()
    assert execution is not None
    first_item = str(uuid4())
    second_item = str(uuid4())
    if first_item < second_item:
        first_item, second_item = second_item, first_item
    hashes = ["a" * 64, "b" * 64]
    pause_id, approval_request_id = str(uuid4()), str(uuid4())
    worker.store.save_pause_snapshot(
        pause_id=pause_id,
        execution_id=command["execution_id"],
        generation=1,
        cancellation_epoch=0,
        approval_request_id=approval_request_id,
        approval_batch_id=str(uuid4()),
        item_ids=[first_item, second_item],
        arguments_hashes=hashes,
    )
    # 原始申请顺序故意与 UUID 升序相反，Resume 按协议使用升序。
    decisions = [
        {
            "item_id": second_item,
            "decision": "denied",
            "arguments_sha256": hashes[1],
            "decided_at": "2026-10-31T08:31:00Z",
        },
        {
            "item_id": first_item,
            "decision": "approved",
            "arguments_sha256": hashes[0],
            "decided_at": "2026-10-31T08:31:00Z",
        },
    ]
    decisions.sort(key=lambda item: item["item_id"])
    resume = {
        "protocol_version": "1.0",
        "command_id": str(uuid4()),
        "execution_id": command["execution_id"],
        "generation": 1,
        "cancellation_epoch": 0,
        "pause_id": pause_id,
        "approval_request_id": approval_request_id,
        "approval_batch_id": str(uuid4()),
        "decisions": decisions,
        "decision_set_sha256": hashlib.sha256(rfc8785.dumps(decisions)).hexdigest(),
        "execution_grant": command["execution_grant"],
    }
    accepted, status = await worker._accept_resume(resume, "resume-hash")
    assert status == 202 and accepted.replayed is False
    await worker.stop()
