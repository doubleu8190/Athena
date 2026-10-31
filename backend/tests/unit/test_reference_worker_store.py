"""参考 Worker SQLite 持久收件、取消栅栏与 outbox 回归。"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from reference_worker.store import WorkerStore


def _body(value: object) -> str:
    """测试中使用固定 JSON 作为持久 Worker 请求。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def test_command_receipt_and_execution_survive_store_reopen(tmp_path) -> None:
    """重开数据库后同命令重放使用原收件回执并继续原执行。"""
    database = tmp_path / "worker.sqlite3"
    worker = WorkerStore(database)
    command_id = str(uuid4())
    request = {
        "protocol_version": "1.0",
        "command_id": command_id,
        "execution_id": "execution-1",
        "task_id": "task-1",
        "generation": 1,
        "operation_id": str(uuid4()),
        "input": {"hello": "world"},
    }
    digest = hashlib.sha256(_body(request).encode()).hexdigest()
    first = worker.accept_command(
        command_id=command_id,
        request_hash=digest,
        command_type="task",
        request=request,
        execution_id="execution-1",
        task_id="task-1",
        generation=1,
        operation_id=str(request["operation_id"]),
        cancellation_epoch=0,
        receipt_id="receipt-1",
    )
    restarted = WorkerStore(database)
    replay = restarted.accept_command(
        command_id=command_id,
        request_hash=digest,
        command_type="task",
        request=request,
        execution_id="execution-1",
        task_id="task-1",
        generation=1,
        operation_id=str(request["operation_id"]),
        cancellation_epoch=0,
        receipt_id="receipt-2",
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.receipt == first.receipt
    claimed = restarted.claim_execution()
    assert claimed is not None
    assert claimed["execution_id"] == "execution-1"
    assert claimed["request"] == request


def test_cancel_fence_ignores_late_old_task_and_replays_cancel_receipt(tmp_path) -> None:
    """任务取消先落盘时，迟到旧任务不会再排队且取消回执保持稳定。"""
    store = WorkerStore(tmp_path / "worker.sqlite3")
    command_id = str(uuid4())
    execution_id = str(uuid4())
    operation_id = str(uuid4())
    task_request = {
        "protocol_version": "1.0",
        "command_id": command_id,
        "execution_id": execution_id,
        "task_id": "task-cancelled",
        "generation": 1,
        "operation_id": operation_id,
        "input": {},
    }
    store.accept_command(
        command_id=command_id,
        request_hash="task-hash",
        command_type="task",
        request=task_request,
        execution_id=execution_id,
        task_id="task-cancelled",
        generation=1,
        operation_id=operation_id,
        cancellation_epoch=1,
        receipt_id="task-receipt",
    )
    cancel_id = str(uuid4())
    cancel_request = {
        "protocol_version": "1.0",
        "command_id": cancel_id,
        "execution_id": execution_id,
        "generation": 1,
        "cancellation_epoch": 1,
        "reason": "task_cancelled",
        "task_id": "task-cancelled",
    }
    cancel_digest = "cancel-body-hash"
    receipt = store.accept_cancel(
        command_id=cancel_id,
        request_hash=cancel_digest,
        request=cancel_request,
        task_id="task-cancelled",
        execution_id=execution_id,
        generation=1,
        cancellation_epoch=1,
        reason="task_cancelled",
        receipt_id="cancel-receipt",
    )
    replay = WorkerStore(store._database_path).accept_cancel(
        command_id=cancel_id,
        request_hash=cancel_digest,
        request=cancel_request,
        task_id="task-cancelled",
        execution_id=execution_id,
        generation=1,
        cancellation_epoch=1,
        reason="task_cancelled",
        receipt_id="different-receipt",
    )
    late = store.accept_command(
        command_id=str(uuid4()),
        request_hash="late-task-hash",
        command_type="task",
        request={**task_request, "command_id": str(uuid4())},
        execution_id=str(uuid4()),
        task_id="task-cancelled",
        generation=1,
        operation_id=str(uuid4()),
        cancellation_epoch=1,
        receipt_id="late-receipt",
    )

    assert receipt.receipt == replay.receipt
    assert replay.replayed is True
    assert late.ignored is True
    assert store.claim_execution() is None


def test_cancel_before_task_persists_execution_block_and_confirms_stop(tmp_path) -> None:
    """未知 Execution 的取消仍持久收件，之后到达的任务永远不能启动。"""
    store = WorkerStore(tmp_path / "worker.sqlite3")
    execution_id = str(uuid4())
    cancel_id = str(uuid4())
    cancel_request = {
        "protocol_version": "1.0",
        "command_id": cancel_id,
        "execution_id": execution_id,
        "generation": 4,
        "cancellation_epoch": 2,
        "reason": "task_cancelled",
    }
    receipt = store.accept_cancel(
        command_id=cancel_id,
        request_hash="cancel-hash",
        request=cancel_request,
        task_id=None,
        execution_id=execution_id,
        generation=4,
        cancellation_epoch=2,
        reason="task_cancelled",
        receipt_id="cancel-receipt",
    )
    assert receipt.receipt["accepted"] is True
    assert store.claim_outbox(10**12)["kind"] == "stop_confirmation"

    task_id = "late-task"
    late = store.accept_command(
        command_id=str(uuid4()),
        request_hash="late-hash",
        command_type="task",
        request={"execution_id": execution_id, "task_id": task_id},
        execution_id=execution_id,
        task_id=task_id,
        generation=4,
        operation_id=str(uuid4()),
        cancellation_epoch=2,
        receipt_id="late-receipt",
    )

    assert late.ignored is True
    assert store.claim_execution() is None


def test_running_cancel_waits_for_stop_finalization_and_recovers_after_restart(tmp_path) -> None:
    """运行中取消不提前发停止确认；重启后可补建未完成确认。"""
    database = tmp_path / "worker.sqlite3"
    store = WorkerStore(database)
    command_id = str(uuid4())
    execution_id = str(uuid4())
    task_id = str(uuid4())
    request = {
        "protocol_version": "1.0",
        "command_id": command_id,
        "execution_id": execution_id,
        "task_id": task_id,
        "generation": 1,
        "operation_id": str(uuid4()),
    }
    store.accept_command(
        command_id=str(uuid4()), request_hash="task-hash", command_type="task",
        request=request, execution_id=execution_id, task_id=task_id, generation=1,
        operation_id=request["operation_id"], cancellation_epoch=1, receipt_id="task-receipt",
    )
    assert store.claim_execution() is not None
    cancel_request = {
        "protocol_version": "1.0", "command_id": command_id,
        "execution_id": execution_id, "generation": 1,
        "cancellation_epoch": 1, "reason": "task_cancelled",
    }
    store.accept_cancel(
        command_id=command_id, request_hash="cancel-hash", request=cancel_request,
        task_id=task_id, execution_id=execution_id, generation=1,
        cancellation_epoch=1, reason="task_cancelled", receipt_id="cancel-receipt",
    )
    assert store.claim_outbox(10**12) is None

    restarted = WorkerStore(database)
    stop = restarted.claim_outbox(10**12)
    assert stop is not None
    assert stop["kind"] == "stop_confirmation"
    assert stop["body"]["cancel_command_id"] == command_id


def test_logical_cancel_waits_for_running_sibling_even_when_target_completed(tmp_path) -> None:
    """取消目标已完成但同 Task 的另一代仍运行时，先等待运行项停止。"""
    store = WorkerStore(tmp_path / "worker.sqlite3")
    task_id = str(uuid4())
    completed_execution = str(uuid4())
    running_execution = str(uuid4())

    def accept(execution_id: str, generation: int) -> None:
        request = {
            "protocol_version": "1.0",
            "command_id": str(uuid4()),
            "execution_id": execution_id,
            "task_id": task_id,
            "generation": generation,
            "operation_id": str(uuid4()),
        }
        store.accept_command(
            command_id=request["command_id"], request_hash=f"hash-{generation}",
            command_type="task", request=request, execution_id=execution_id,
            task_id=task_id, generation=generation,
            operation_id=request["operation_id"], cancellation_epoch=1,
            receipt_id=f"receipt-{generation}",
        )

    accept(completed_execution, 1)
    accept(running_execution, 2)
    assert store.claim_execution() is not None
    assert store.claim_execution() is not None
    store.complete_execution(
        execution_id=completed_execution, generation=1, output={"ok": True},
        result_id=str(uuid4()),
    )
    result_outbox = store.claim_outbox(10**12)
    assert result_outbox is not None and result_outbox["kind"] == "result"
    store.acknowledge_outbox(int(result_outbox["outbox_id"]))
    cancel_id = str(uuid4())
    cancel_request = {
        "protocol_version": "1.0", "command_id": cancel_id,
        "execution_id": completed_execution, "generation": 1,
        "cancellation_epoch": 1, "reason": "task_cancelled",
    }
    store.accept_cancel(
        command_id=cancel_id, request_hash="cancel-hash", request=cancel_request,
        task_id=task_id, execution_id=completed_execution, generation=1,
        cancellation_epoch=1, reason="task_cancelled", receipt_id="cancel-receipt",
    )

    assert store.claim_outbox(10**12) is None
    targets = store.cancel_targets(completed_execution, 1, "task_cancelled")
    assert (running_execution, 2) in targets


@pytest.mark.parametrize(
    ("initial", "maximum", "jitter", "accepted"),
    [
        (1.0, 60.0, 0.2, True),
        (60.0, 3600.0, 1.0, True),
        (60.1, 100.0, 0.2, False),
        (1.0, 0.5, 0.2, False),
        (1.0, 2.0, 1.1, False),
    ],
)
def test_retry_configuration_matches_protocol_bounds(
    tmp_path, initial: float, maximum: float, jitter: float, accepted: bool
) -> None:
    """Worker 启动配置拒绝超出 S1 的结果重投参数。"""
    from datetime import UTC

    from reference_worker.config import ReferenceWorkerConfig

    tls = tmp_path / "worker.pem"
    key = tmp_path / "worker.key"
    ca = tmp_path / "ca.pem"
    for file in (tls, key, ca):
        file.touch()
    config_values = {
        "orchestrator_url": "https://athena.test",
        "public_base_url": "https://worker.test",
        "service_token": "worker-token",
        "orchestrator_token": "orchestrator-token",
        "instance_key": uuid4(),
        "database_path": tmp_path / "worker.sqlite3",
        "capability_path": tmp_path / "capability.json",
        "tls_certificate_path": tls,
        "tls_private_key_path": key,
        "ca_certificate_path": ca,
        "result_retry_initial_seconds": initial,
        "result_retry_max_seconds": maximum,
        "result_retry_jitter_ratio": jitter,
    }
    if accepted:
        assert ReferenceWorkerConfig(**config_values).result_retry_jitter_ratio == jitter
    else:
        with pytest.raises(ValueError):
            ReferenceWorkerConfig(**config_values)
