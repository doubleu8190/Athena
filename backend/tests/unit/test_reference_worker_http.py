"""参考 Worker HTTP 收件、身份校验及本地能力输入校验。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from uuid import uuid4

import httpx
import pytest

from interfaces.http.worker_protocol import WorkerProtocolHttp
from reference_worker.config import ReferenceWorkerConfig
from reference_worker.runtime import ReferenceWorker


def _config(tmp_path) -> ReferenceWorkerConfig:
    """建立仅用于 ASGI 路由测试的 Worker 配置。"""
    capability = tmp_path / "capability.json"
    capability.write_text(
        json.dumps(
            {
                "can_do": ["Echo a JSON object."],
                "cannot_do": ["Run arbitrary code."],
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"echo": {"type": "object"}},
                    "required": ["echo"],
                    "additionalProperties": False,
                },
            }
        ),
        encoding="utf-8",
    )
    certificate = tmp_path / "worker.crt"
    private_key = tmp_path / "worker.key"
    ca_certificate = tmp_path / "ca.crt"
    for file in (certificate, private_key, ca_certificate):
        file.touch()
    return ReferenceWorkerConfig(
        orchestrator_url="https://athena.test",
        public_base_url="https://worker.test",
        service_token="worker-service-token",
        orchestrator_token="athena-orchestrator-token",
        instance_key=uuid4(),
        database_path=tmp_path / "worker.sqlite3",
        capability_path=capability,
        tls_certificate_path=certificate,
        tls_private_key_path=private_key,
        ca_certificate_path=ca_certificate,
    )


def _command(*, command_id: str | None = None, input_body: object = None) -> dict[str, object]:
    """返回经过官方 TaskCommand Schema 校验的确定性任务。"""
    return {
        "protocol_version": "1.0",
        "command_id": command_id or str(uuid4()),
        "run_id": str(uuid4()),
        "plan_id": str(uuid4()),
        "task_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "generation": 1,
        "cancellation_epoch": 0,
        "operation_id": str(uuid4()),
        "contract_id": "capability-contract",
        "contract_revision": 1,
        "objective": "Echo the JSON object.",
        "acceptance_criteria": "The output contains the original input.",
        "input": {} if input_body is None else input_body,
        "execution_grant": {
            "token": "a-valid-opaque-execution-grant-token",
            "expires_at": "2026-10-30T08:30:00Z",
        },
    }


@pytest.mark.asyncio
async def test_task_receipt_replays_when_worker_unavailable_and_contract_changes(tmp_path) -> None:
    """旧命令在 Worker 暂停接单且契约更新后仍返回原回执。"""
    config = _config(tmp_path)
    worker = ReferenceWorker(config)
    registration = {
        "request_fingerprint": "local-fingerprint",
        "registration_request_id": str(uuid4()),
        "response": {
            "worker_instance_id": "worker-1",
            "worker_instance_key": str(config.instance_key),
            "contract_id": "capability-contract",
            "contract_revision": 1,
            "status": "inactive",
            "registered_at": "2026-10-30T08:00:00Z",
        },
    }
    worker.store.set_setting("registration", registration)
    worker._registered = True
    worker._accepting_tasks = True
    protocol = WorkerProtocolHttp({"athena-orchestrator": config.orchestrator_token})
    app = worker.create_app(protocol)
    transport = httpx.ASGITransport(app=app)
    headers = {
        "Authorization": f"Bearer {config.orchestrator_token}",
        "X-Athena-Protocol-Version": "1.0",
        "Content-Type": "application/json; charset=utf-8",
    }
    command = _command(input_body={"query": "test"})
    headers["Idempotency-Key"] = str(command["command_id"])

    async with httpx.AsyncClient(transport=transport, base_url="https://worker.test") as client:
        first = await client.post("/worker-api/v1/tasks", headers=headers, json=command)
        worker._accepting_tasks = False
        registration["response"]["contract_revision"] = 2
        worker.store.set_setting("registration", registration)
        replay = await client.post("/worker-api/v1/tasks", headers=headers, json=command)
        altered = {**command, "input": {"query": "different"}}
        conflict = await client.post("/worker-api/v1/tasks", headers=headers, json=altered)

    assert first.status_code == 202
    assert first.json()["disposition"] == "accepted"
    assert replay.status_code == 200
    assert replay.json()["disposition"] == "duplicate"
    assert replay.json()["receipt_id"] == first.json()["receipt_id"]
    assert replay.json()["received_at"] == first.json()["received_at"]
    assert conflict.status_code == 409
    assert worker.store.claim_execution() is not None
    await worker.stop()


@pytest.mark.asyncio
async def test_accepted_task_keeps_contract_snapshot_after_worker_restart(tmp_path) -> None:
    """重启后能力文件变化不改变已收件任务的输出契约。"""
    config = _config(tmp_path)
    worker = ReferenceWorker(config)
    worker.store.set_setting(
        "registration",
        {
            "request_fingerprint": "local-fingerprint",
            "registration_request_id": str(uuid4()),
            "response": {
                "worker_instance_id": "worker-1",
                "worker_instance_key": str(config.instance_key),
                "contract_id": "capability-contract",
                "contract_revision": 1,
                "status": "ready",
                "registered_at": "2026-10-30T08:00:00Z",
            },
        },
    )
    worker._registered = True
    worker._accepting_tasks = True
    command = _command(input_body={"query": "accepted under revision one"})
    app = worker.create_app(
        WorkerProtocolHttp({"athena-orchestrator": config.orchestrator_token})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://worker.test"
    ) as client:
        response = await client.post(
            "/worker-api/v1/tasks",
            headers={
                "Authorization": f"Bearer {config.orchestrator_token}",
                "X-Athena-Protocol-Version": "1.0",
                "Content-Type": "application/json; charset=utf-8",
                "Idempotency-Key": str(command["command_id"]),
            },
            json=command,
        )
    assert response.status_code == 202

    capability = json.loads(config.capability_path.read_text(encoding="utf-8"))
    capability["output_schema"] = {
        "type": "object",
        "properties": {"new_output": {"type": "string"}},
        "required": ["new_output"],
        "additionalProperties": False,
    }
    config.capability_path.write_text(json.dumps(capability), encoding="utf-8")
    restarted = ReferenceWorker(config)
    execution = restarted.store.claim_execution()
    assert execution is not None

    await restarted._execute(execution)
    result = restarted.store.claim_outbox(10**12)

    assert result is not None
    assert result["body"]["status"] == "completed"
    assert result["body"]["output"] == {"echo": {"query": "accepted under revision one"}}
    await worker.stop()
    await restarted.stop()


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_retryable"),
    [
        ("get_timeout", "WORKER_UPSTREAM_TIMEOUT", True),
        ("get_503", "WORKER_RESOURCE_UNAVAILABLE", True),
        ("get_403", "WORKER_PERMISSION_DENIED", False),
        ("get_digest", "WORKER_RESOURCE_UNAVAILABLE", True),
        ("put_timeout", "WORKER_UPSTREAM_TIMEOUT", True),
        ("put_503", "WORKER_RESOURCE_UNAVAILABLE", True),
        ("put_403", "WORKER_PERMISSION_DENIED", False),
        ("put_digest", "WORKER_RESOURCE_UNAVAILABLE", True),
        ("output_schema", "WORKER_OUTPUT_INVALID", False),
    ],
)
@pytest.mark.asyncio
async def test_resource_failures_use_s1_error_classification(
    tmp_path, failure, expected_code: str, expected_retryable: bool
) -> None:
    """GET/PUT 的超时、状态和内容摘要故障映射为稳定 S1 错误。"""
    config = _config(tmp_path)
    capability = json.loads(config.capability_path.read_text(encoding="utf-8"))
    capability["input_schema"] = {
        "type": "object",
        "properties": {"resource_reference": {"type": "object"}},
        "required": ["resource_reference"],
        "additionalProperties": False,
    }
    capability["output_schema"] = {
        "type": "object",
        "properties": {
            "different": {"type": "string"}
        } if failure == "output_schema" else {"resource": {"type": "object"}},
        "required": ["different"] if failure == "output_schema" else ["resource"],
        "additionalProperties": False,
    }
    config.capability_path.write_text(json.dumps(capability), encoding="utf-8")
    worker = ReferenceWorker(config)
    worker.store.set_setting(
        "registration",
        {
            "request_fingerprint": "local-fingerprint",
            "registration_request_id": str(uuid4()),
            "response": {
                "worker_instance_id": "worker-1",
                "worker_instance_key": str(config.instance_key),
                "contract_id": "capability-contract",
                "contract_revision": 1,
                "status": "ready",
                "registered_at": "2026-10-30T08:00:00Z",
            },
        },
    )
    worker._registered = True
    worker._accepting_tasks = True
    resource_bytes = b"authorized-resource"
    digest = hashlib.sha256(resource_bytes).hexdigest()
    reference = {
        "resource_id": str(uuid4()),
        "media_type": "application/octet-stream",
        "size_bytes": len(resource_bytes),
        "sha256": digest,
    }

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if failure == "get_timeout":
                raise httpx.ReadTimeout("simulated resource timeout", request=request)
            if failure == "get_503":
                return httpx.Response(503, json={"error": "unavailable"})
            if failure == "get_403":
                return httpx.Response(403, json={"error": "denied"})
            return httpx.Response(
                200,
                content=resource_bytes,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(resource_bytes)),
                    "Content-SHA256": "0" * 64 if failure == "get_digest" else digest,
                },
            )
        if failure == "put_timeout":
            raise httpx.ReadTimeout("simulated resource upload timeout", request=request)
        if failure == "put_403":
            return httpx.Response(403, json={"error": "denied"})
        if failure in {"put_digest", "output_schema"}:
            return httpx.Response(
                200,
                json={
                    "protocol_version": "1.0",
                    "resource_id": request.url.path.rsplit("/", 1)[-1],
                    "media_type": "application/octet-stream",
                    "size_bytes": len(resource_bytes),
                    "sha256": "0" * 64 if failure == "put_digest" else digest,
                },
            )
        return httpx.Response(503, json={"error": "unavailable"})

    worker._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    command = _command(input_body={"resource_reference": reference})
    command["execution_grant"] = {
        "token": "opaque-test-execution-grant-token",
        "expires_at": "2026-10-30T08:30:00Z",
    }
    await worker._accept_task(command, f"hash-{failure}")
    execution = worker.store.claim_execution()
    assert execution is not None
    await worker._execute(execution)
    result = worker.store.claim_outbox(10**12)
    assert result is not None
    assert result["body"]["error"]["code"] == expected_code
    assert result["body"]["error"]["retryable"] is expected_retryable
    await worker.stop()


@pytest.mark.asyncio
async def test_cancel_replay_retries_failed_finalization_once(tmp_path, monkeypatch) -> None:
    """首次停止确认写入失败后，同进程重放会补齐唯一 outbox 项。"""
    worker = ReferenceWorker(_config(tmp_path))
    task = _command(input_body={"query": "cancel this execution"})
    worker.store.accept_command(
        command_id=str(task["command_id"]),
        request_hash="task-hash-for-cancel-finalization",
        command_type="task",
        request=task,
        execution_id=str(task["execution_id"]),
        task_id=str(task["task_id"]),
        generation=int(task["generation"]),
        operation_id=str(task["operation_id"]),
        cancellation_epoch=0,
        receipt_id="task-receipt-for-cancel-finalization",
    )
    assert worker.store.claim_execution() is not None
    request = {
        "protocol_version": "1.0",
        "command_id": str(uuid4()),
        "execution_id": task["execution_id"],
        "generation": 1,
        "cancellation_epoch": 1,
        "reason": "task_cancelled",
    }
    original_finalize = worker.store.finalize_cancel
    attempts = 0

    def fail_once(command_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated_finalize_failure")
        original_finalize(command_id)

    monkeypatch.setattr(worker.store, "finalize_cancel", fail_once)
    with pytest.raises(RuntimeError, match="simulated_finalize_failure"):
        await worker._accept_cancel(request, "stable-cancel-hash")
    with sqlite3.connect(worker.config.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE kind='stop_confirmation'"
        ).fetchone()[0] == 0

    receipt, status = await worker._accept_cancel(request, "stable-cancel-hash")
    stop_item = worker.store.claim_outbox(10**12)
    with sqlite3.connect(worker.config.database_path) as connection:
        stop_count = connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE kind='stop_confirmation'"
        ).fetchone()[0]

    assert status == 202
    assert receipt.replayed is True
    assert stop_item is not None
    assert stop_item["kind"] == "stop_confirmation"
    assert stop_count == 1
    assert attempts == 2
    await worker.stop()


@pytest.mark.asyncio
async def test_protocol_authentication_and_capability_input_are_enforced(tmp_path) -> None:
    """错误服务身份和不符合冻结 input_schema 的任务在收件前拒绝。"""
    config = _config(tmp_path)
    worker = ReferenceWorker(config)
    worker.store.set_setting(
        "registration",
        {
            "request_fingerprint": "local-fingerprint",
            "registration_request_id": str(uuid4()),
            "response": {
                "worker_instance_id": "worker-1",
                "worker_instance_key": str(config.instance_key),
                "contract_id": "capability-contract",
                "contract_revision": 1,
                "status": "inactive",
                "registered_at": "2026-10-30T08:00:00Z",
            },
        },
    )
    worker._registered = True
    worker._accepting_tasks = True
    app = worker.create_app(
        WorkerProtocolHttp({"athena-orchestrator": config.orchestrator_token})
    )
    transport = httpx.ASGITransport(app=app)
    base_headers = {
        "Authorization": f"Bearer {config.orchestrator_token}",
        "X-Athena-Protocol-Version": "1.0",
        "Content-Type": "application/json; charset=utf-8",
    }
    command = _command(input_body={"unexpected": "schema requires query"})

    async with httpx.AsyncClient(transport=transport, base_url="https://worker.test") as client:
        unauthenticated = await client.post(
            "/worker-api/v1/tasks",
            headers={**base_headers, "Authorization": "Bearer invalid-token",
                     "Idempotency-Key": str(command["command_id"])},
            json=command,
        )
        invalid_input = await client.post(
            "/worker-api/v1/tasks",
            headers={**base_headers, "Idempotency-Key": str(command["command_id"])},
            json=command,
        )
        health = await client.get(
            "/worker-api/v1/health",
            headers=base_headers,
        )

    assert unauthenticated.status_code == 401
    assert invalid_input.status_code == 422
    assert invalid_input.json()["code"] == "TASK_INPUT_INVALID"
    assert worker.store.claim_execution() is None
    assert health.status_code == 200
    assert health.json()["worker_instance_key"] == str(config.instance_key)


@pytest.mark.asyncio
async def test_running_cancel_awaits_execution_before_stop_confirmation(tmp_path) -> None:
    """取消正在运行的协程后才生成物理停止确认。"""
    worker = ReferenceWorker(_config(tmp_path))
    command = _command(input_body={"query": "test"})
    worker.store.accept_command(
        command_id=str(command["command_id"]), request_hash="task-hash",
        command_type="task", request=command,
        execution_id=str(command["execution_id"]), task_id=str(command["task_id"]),
        generation=int(command["generation"]), operation_id=str(command["operation_id"]),
        cancellation_epoch=1, receipt_id="task-receipt",
    )
    assert worker.store.claim_execution() is not None
    key = (str(command["execution_id"]), int(command["generation"]))
    stopped = asyncio.Event()

    async def running_execution() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    execution_task = asyncio.create_task(running_execution())
    worker._execution_tasks.add(execution_task)
    worker._execution_tasks_by_key[key] = execution_task
    await asyncio.sleep(0)
    cancel = {
        "protocol_version": "1.0",
        "command_id": str(uuid4()),
        "execution_id": str(command["execution_id"]),
        "generation": int(command["generation"]),
        "cancellation_epoch": 1,
        "reason": "task_cancelled",
    }

    receipt, status = await worker._accept_cancel(cancel, "cancel-hash")
    stop_item = worker.store.claim_outbox(10**12)

    assert status == 202
    assert receipt.receipt["accepted"] is True
    assert stopped.is_set()
    assert execution_task.done()
    assert stop_item is not None
    assert stop_item["kind"] == "stop_confirmation"
    await worker.stop()


@pytest.mark.asyncio
async def test_cancel_before_task_is_accepted_and_replayed(tmp_path) -> None:
    """未知 Execution 的取消返回稳定 202，并阻止迟到任务进入执行队列。"""
    config = _config(tmp_path)
    worker = ReferenceWorker(config)
    app = worker.create_app(
        WorkerProtocolHttp({"athena-orchestrator": config.orchestrator_token})
    )
    headers = {
        "Authorization": f"Bearer {config.orchestrator_token}",
        "X-Athena-Protocol-Version": "1.0",
        "Content-Type": "application/json; charset=utf-8",
    }
    cancel = {
        "protocol_version": "1.0",
        "command_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "generation": 1,
        "cancellation_epoch": 1,
        "reason": "task_cancelled",
    }
    headers["Idempotency-Key"] = cancel["command_id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://worker.test"
    ) as client:
        first = await client.post(
            f"/worker-api/v1/executions/{cancel['execution_id']}/cancel",
            headers=headers,
            json=cancel,
        )
        replay = await client.post(
            f"/worker-api/v1/executions/{cancel['execution_id']}/cancel",
            headers=headers,
            json=cancel,
        )

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert worker.store.claim_outbox(10**12)["kind"] == "stop_confirmation"
    await worker.stop()
