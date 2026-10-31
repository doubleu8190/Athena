"""Worker 本地 SQLite 收件、执行和 outbox 的持久存储。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import WorkerContractSnapshot


def _utc_now() -> str:
    """返回可按 UTC 时间排序的 RFC 3339 时间。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: object) -> str:
    """将协议对象编码为稳定 JSON。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _command_receipt(
    receipt_id: str, command_id: str, execution_id: str, generation: int
) -> dict[str, object]:
    """构造符合 S1 的持久命令回执。"""
    return {
        "protocol_version": "1.0",
        "command_id": command_id,
        "receipt_id": receipt_id,
        "execution_id": execution_id,
        "generation": generation,
        "disposition": "accepted",
        "received_at": _utc_now(),
    }


@dataclass(frozen=True, slots=True)
class StoredReceipt:
    """本地持久接收记录返回的稳定业务回执。"""

    receipt: dict[str, object]
    replayed: bool
    ignored: bool = False


class WorkerStore:
    """使用 SQLite 事务保存命令收件、状态和未确认结果。"""

    def __init__(self, database_path: Path) -> None:
        """打开本地数据库并创建首次启动所需的表。

        参数：
        - `database_path`（Path）：仅当前 Worker 使用的本地持久数据库。

        返回：无。

        异常：
        - sqlite3.Error：数据库不可读写或 schema 初始化失败时抛出。
        """
        self._database_path = database_path
        database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS local_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS protocol_commands (
                    command_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    command_type TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    cancellation_epoch INTEGER NOT NULL,
                    task_cancellation_epoch INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    output_json TEXT,
                    result_id TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (execution_id, generation)
                );
                CREATE INDEX IF NOT EXISTS ix_executions_task
                    ON executions(task_id, generation);
                CREATE TABLE IF NOT EXISTS execution_contracts (
                    execution_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    PRIMARY KEY (execution_id, generation)
                );
                CREATE TABLE IF NOT EXISTS task_cancellations (
                    task_id TEXT PRIMARY KEY,
                    cancellation_epoch INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blocked_executions (
                    execution_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    cancellation_epoch INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(execution_id, generation)
                );
                CREATE TABLE IF NOT EXISTS approval_pauses (
                    pause_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    cancellation_epoch INTEGER NOT NULL,
                    approval_request_id TEXT NOT NULL,
                    approval_batch_id TEXT,
                    item_ids_json TEXT NOT NULL,
                    arguments_hashes_json TEXT NOT NULL,
                    decisions_json TEXT,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    body_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                """
            )
            connection.execute(
                "UPDATE executions SET state='queued', updated_at=? WHERE state='running'",
                (_utc_now(),),
            )
            pending_cancels = connection.execute(
                "SELECT command_id,request_json FROM protocol_commands "
                "WHERE command_type='cancel' AND state='accepted'"
            ).fetchall()
            for cancel in pending_cancels:
                self._enqueue_stop_confirmation(
                    connection, json.loads(cancel["request_json"]), _utc_now()
                )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """为一次事务打开独立连接并在结束时关闭。

        参数：无。

        返回：开启外键与超时等待的 SQLite 连接。

        异常：sqlite3.Error 在调用端处理或传播。
        """
        connection = sqlite3.connect(
            self._database_path, timeout=30, isolation_level="IMMEDIATE"
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_setting(self, key: str) -> dict[str, object] | None:
        """读取一个小型 JSON 设置快照。

        参数：
        - `key`（str）：Worker 本地稳定设置名。

        返回：JSON 对象；该设置尚未保存时返回 `None`。

        异常：JSON 内容损坏时由 `json.loads` 抛出。
        """
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM local_settings WHERE key=?", (key,)
            ).fetchone()
            return None if row is None else json.loads(row["value_json"])

    def set_setting(self, key: str, value: dict[str, object]) -> None:
        """原子保存一个 JSON 设置快照。

        参数：
        - `key`（str）：稳定设置名。
        - `value`（dict[str, object]）：可 JSON 编码的设置对象。

        返回：无。

        异常：SQLite 或 JSON 编码错误时抛出。
        """
        serialized = _json(value)
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO local_settings(key,value_json) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, serialized),
            )

    def accept_command(
        self,
        *,
        command_id: str,
        request_hash: str,
        command_type: str,
        request: dict[str, object],
        execution_id: str,
        task_id: str,
        generation: int,
        operation_id: str,
        cancellation_epoch: int,
        receipt_id: str,
        contract_snapshot: WorkerContractSnapshot | None = None,
    ) -> StoredReceipt:
        """持久接收命令并原子创建执行或恢复收件回执。

        参数：
        - `command_id`（str）：v1 UUID 命令身份。
        - `request_hash`（str）：原始 JSON body 的 SHA-256(JCS)。
        - `command_type`（str）：`task` 或 `resume`。
        - `request`（dict[str, object]）：完整已验证请求。
        - `execution_id`（str）：命令目标 Execution。
        - `task_id`（str）：命令所属逻辑 Task。
        - `generation`（int）：命令目标代次。
        - `operation_id`（str）：跨代次稳定副作用身份。
        - `cancellation_epoch`（int）：中心的逻辑取消栅栏代次。
        - `receipt_id`（str）：首次接收生成并持久化的收据身份。

        返回：固定 received_at 的回执及是否为同 body 重放。

        异常：
        - ValueError：命令 ID 内容冲突或旧命令在取消栅栏后到达。
        - sqlite3.IntegrityError：重复逻辑副作用 ID 指向其他执行时抛出。
        """
        with self._connection() as connection:
            previous = connection.execute(
                "SELECT request_hash,receipt_json,state FROM protocol_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise ValueError("command_id_conflict")
                return StoredReceipt(
                    json.loads(previous["receipt_json"]),
                    True,
                    previous["state"] == "ignored",
                )

            fence = connection.execute(
                "SELECT cancellation_epoch FROM task_cancellations WHERE task_id=?",
                (task_id,),
            ).fetchone()
            ignored = fence is not None and int(fence["cancellation_epoch"]) >= cancellation_epoch
            blocked = connection.execute(
                "SELECT cancellation_epoch,reason FROM blocked_executions "
                "WHERE execution_id=? AND generation=?",
                (execution_id, generation),
            ).fetchone()
            ignored = ignored or blocked is not None
            if (
                blocked is not None
                and blocked["reason"] in {"task_cancelled", "run_cancelled"}
            ):
                connection.execute(
                    "INSERT INTO task_cancellations VALUES(?,?,?) "
                    "ON CONFLICT(task_id) DO UPDATE SET "
                    "cancellation_epoch=MAX(cancellation_epoch,excluded.cancellation_epoch),"
                    "updated_at=excluded.updated_at",
                    (task_id, int(blocked["cancellation_epoch"]), _utc_now()),
                )
            receipt = _command_receipt(receipt_id, command_id, execution_id, generation)
            if not ignored and command_type == "task":
                existing = connection.execute(
                    "SELECT operation_id FROM executions WHERE execution_id=? AND generation=?",
                    (execution_id, generation),
                ).fetchone()
                if existing is not None:
                    raise ValueError("execution_command_conflict")
                connection.execute(
                    "INSERT INTO executions VALUES(?,?,?,?,?,?,?,NULL,NULL,?)",
                    (
                        execution_id,
                        generation,
                        task_id,
                        operation_id,
                        cancellation_epoch,
                        0,
                        "queued",
                        _utc_now(),
                    ),
                )
                if contract_snapshot is not None:
                    connection.execute(
                        "INSERT INTO execution_contracts VALUES(?,?,?)",
                        (
                            execution_id,
                            generation,
                            _json(contract_snapshot.model_dump(mode="json")),
                        ),
                    )
            connection.execute(
                "INSERT INTO protocol_commands VALUES(?,?,?,?,?,?,?)",
                (
                    command_id,
                    request_hash,
                    command_type,
                    _json(request),
                    _json(receipt),
                    receipt["received_at"],
                    "ignored" if ignored else "accepted",
                ),
            )
            return StoredReceipt(receipt, False, ignored)

    def accept_cancel(
        self,
        *,
        command_id: str,
        request_hash: str,
        request: dict[str, object],
        task_id: str | None,
        execution_id: str,
        generation: int,
        cancellation_epoch: int,
        reason: str,
        receipt_id: str,
    ) -> StoredReceipt:
        """保存取消收件回执并建立 Task 栅栏或 Execution 屏障。

        参数：
        - `command_id`（str）：稳定 CancelCommand 身份。
        - `request_hash`（str）：未经字段归一化的 JCS 摘要。
        - `request`（dict[str, object]）：完整协议命令。
        - `execution_id`（str）：要停止的旧或当前 Execution。
        - `generation`（int）：被取消的执行代次。
        - `cancellation_epoch`（int）：中心逻辑取消代次。
        - `reason`（str）：逻辑取消或只针对执行的停止原因。
        - `receipt_id`（str）：首次收到命令时生成的稳定回执 ID。

        返回：已持久化的原始 CancelReceipt 和重放标记。

        异常：ValueError：命令 ID 内容冲突时抛出。
        """
        with self._connection() as connection:
            previous = connection.execute(
                "SELECT request_hash,receipt_json FROM protocol_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise ValueError("command_id_conflict")
                return StoredReceipt(json.loads(previous["receipt_json"]), True)
            now = _utc_now()
            command = connection.execute(
                "SELECT task_id,state FROM executions WHERE execution_id=? AND generation=?",
                (execution_id, generation),
            ).fetchone()
            resolved_task_id = task_id or (None if command is None else command["task_id"])
            if task_id is not None and command is not None and command["task_id"] != task_id:
                raise ValueError("cancel_task_identity_mismatch")
            connection.execute(
                "INSERT INTO blocked_executions "
                "(execution_id,generation,cancellation_epoch,reason,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(execution_id,generation) DO UPDATE SET "
                "cancellation_epoch=MAX(cancellation_epoch,excluded.cancellation_epoch),"
                "reason=excluded.reason,updated_at=excluded.updated_at",
                (execution_id, generation, cancellation_epoch, reason, now),
            )
            if reason in {"task_cancelled", "run_cancelled"}:
                running_targets = 0
                if resolved_task_id is not None:
                    running_targets = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM executions WHERE task_id=? "
                            "AND cancellation_epoch<=? AND state='running'",
                            (resolved_task_id, cancellation_epoch),
                        ).fetchone()[0]
                    )
                    connection.execute(
                        "INSERT INTO task_cancellations VALUES(?,?,?) "
                        "ON CONFLICT(task_id) DO UPDATE SET "
                        "cancellation_epoch=MAX(cancellation_epoch,excluded.cancellation_epoch),"
                        "updated_at=excluded.updated_at",
                        (resolved_task_id, cancellation_epoch, now),
                    )
                    connection.execute(
                        "UPDATE executions SET state='cancelled',updated_at=? "
                        "WHERE task_id=? AND cancellation_epoch<=? "
                        "AND state IN ('queued','running','waiting_approval')",
                        (now, resolved_task_id, cancellation_epoch),
                    )
            else:
                connection.execute(
                    "UPDATE executions SET state='stopped',updated_at=? "
                    "WHERE execution_id=? AND generation=? "
                    "AND state IN ('queued','running','waiting_approval')",
                    (now, execution_id, generation),
                )
            receipt = {
                "protocol_version": "1.0",
                "command_id": command_id,
                "receipt_id": receipt_id,
                "execution_id": execution_id,
                "generation": generation,
                "cancellation_epoch": cancellation_epoch,
                "accepted": True,
                "reason": reason,
                "stop_confirmation_required": True,
                "received_at": now,
            }
            connection.execute(
                "INSERT INTO protocol_commands VALUES(?,?,?,?,?,?,?)",
                (
                    command_id,
                    request_hash,
                    "cancel",
                    _json(request),
                    _json(receipt),
                    now,
                    "accepted",
                ),
            )
            # 未知/排队执行已被持久封锁，不能启动；运行中的执行须等协程退出后确认。
            if (
                command is None
                or command["state"] != "running"
            ) and not (
                reason in {"task_cancelled", "run_cancelled"}
                and resolved_task_id is not None
                and running_targets > 0
            ):
                self._enqueue_stop_confirmation(connection, request, now)
            return StoredReceipt(receipt, False)

    def cancel_targets(
        self, execution_id: str, generation: int, reason: str
    ) -> list[tuple[str, int]]:
        """返回本次取消必须等待物理停止的运行中执行身份。"""
        with self._connection() as connection:
            if reason not in {"task_cancelled", "run_cancelled"}:
                rows = connection.execute(
                    "SELECT execution_id,generation FROM executions "
                    "WHERE execution_id=? AND generation=? "
                    "AND state IN ('cancelled','stopped')",
                    (execution_id, generation),
                ).fetchall()
            else:
                row = connection.execute(
                    "SELECT task_id FROM executions WHERE execution_id=? AND generation=?",
                    (execution_id, generation),
                ).fetchone()
                if row is None:
                    return []
                rows = connection.execute(
                    "SELECT execution_id,generation FROM executions "
                    "WHERE task_id=? AND state='cancelled'",
                    (row["task_id"],),
                ).fetchall()
            return [(str(row["execution_id"]), int(row["generation"])) for row in rows]

    def finalize_cancel(self, command_id: str) -> None:
        """在已等待执行协程退出后写入稳定的 StopConfirmation outbox。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT request_json FROM protocol_commands WHERE command_id=? "
                "AND command_type='cancel'",
                (command_id,),
            ).fetchone()
            if row is None:
                raise LookupError("worker_cancel_command_unknown")
            self._enqueue_stop_confirmation(
                connection, json.loads(row["request_json"]), _utc_now()
            )

    def replay_command(
        self, command_id: str, request_hash: str
    ) -> StoredReceipt | None:
        """在检查当前暂停或执行状态前读取幂等收件回执。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT request_hash,receipt_json,state FROM protocol_commands "
                "WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise ValueError("command_id_conflict")
            return StoredReceipt(
                json.loads(row["receipt_json"]),
                True,
                row["state"] == "ignored",
            )

    def claim_execution(self) -> dict[str, object] | None:
        """以单笔 SQLite 事务领取一个待运行的纯 echo 执行。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT execution_id,generation,task_id,cancellation_epoch "
                "FROM executions WHERE state='queued' ORDER BY updated_at,execution_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            result = connection.execute(
                "UPDATE executions SET state='running',updated_at=? "
                "WHERE execution_id=? AND generation=? AND state='queued'",
                (_utc_now(), row["execution_id"], row["generation"]),
            )
            if result.rowcount != 1:
                return None
            command = connection.execute(
                "SELECT request_json FROM protocol_commands WHERE command_type='task' "
                "AND state='accepted' AND json_extract(request_json,'$.execution_id')=? "
                "ORDER BY received_at LIMIT 1",
                (row["execution_id"],),
            ).fetchone()
            if command is None:
                connection.execute(
                    "UPDATE executions SET state='failed',updated_at=? "
                    "WHERE execution_id=? AND generation=?",
                    (_utc_now(), row["execution_id"], row["generation"]),
                )
                return None
            snapshot = connection.execute(
                "SELECT snapshot_json FROM execution_contracts "
                "WHERE execution_id=? AND generation=?",
                (row["execution_id"], row["generation"]),
            ).fetchone()
            return {
                "execution_id": row["execution_id"],
                "generation": int(row["generation"]),
                "task_id": row["task_id"],
                "cancellation_epoch": int(row["cancellation_epoch"]),
                "request": json.loads(command["request_json"]),
                "contract_snapshot": (
                    None
                    if snapshot is None
                    else WorkerContractSnapshot.model_validate_json(
                        snapshot["snapshot_json"]
                    )
                ),
            }

    def complete_execution(
        self,
        *,
        execution_id: str,
        generation: int,
        output: dict[str, object],
        result_id: str,
    ) -> dict[str, object] | None:
        """原子保存纯 echo 结果和待提交 ResultSubmission。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT task_id,cancellation_epoch,state FROM executions "
                "WHERE execution_id=? AND generation=?",
                (execution_id, generation),
            ).fetchone()
            if row is None or row["state"] != "running":
                return None
            fence = connection.execute(
                "SELECT cancellation_epoch FROM task_cancellations WHERE task_id=?",
                (row["task_id"],),
            ).fetchone()
            if fence is not None and int(fence["cancellation_epoch"]) >= int(row["cancellation_epoch"]):
                connection.execute(
                    "UPDATE executions SET state='cancelled',updated_at=? "
                    "WHERE execution_id=? AND generation=?",
                    (_utc_now(), execution_id, generation),
                )
                return None
            result_body: dict[str, object] = {
                "protocol_version": "1.0",
                "result_id": result_id,
                "generation": generation,
                "status": "completed",
                "output": output,
            }
            now = _utc_now()
            connection.execute(
                "UPDATE executions SET state='completed',output_json=?,result_id=?,updated_at=? "
                "WHERE execution_id=? AND generation=? AND state='running'",
                (_json(output), result_id, now, execution_id, generation),
            )
            connection.execute(
                "INSERT OR IGNORE INTO outbox(kind,dedupe_key,body_json) VALUES(?,?,?)",
                ("result", f"result:{execution_id}:{generation}", _json(result_body)),
            )
            return result_body

    def claim_outbox(self, now_epoch: float) -> dict[str, object] | None:
        """领取到期的结果或停止确认投递项。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT outbox_id,kind,dedupe_key,body_json,attempt FROM outbox "
                "WHERE state='pending' AND available_at<=? ORDER BY outbox_id LIMIT 1",
                (now_epoch,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE outbox SET attempt=attempt+1 WHERE outbox_id=? AND state='pending'",
                (row["outbox_id"],),
            )
            return {
                "outbox_id": int(row["outbox_id"]),
                "kind": row["kind"],
                "dedupe_key": row["dedupe_key"],
                "body": json.loads(row["body_json"]),
                "attempt": int(row["attempt"]) + 1,
            }

    def acknowledge_outbox(self, outbox_id: int) -> None:
        """标记获中心成功确认的 outbox 项已完成。"""
        with self._connection() as connection:
            connection.execute(
                "UPDATE outbox SET state='delivered' WHERE outbox_id=?", (outbox_id,)
            )

    def retry_outbox(self, outbox_id: int, *, available_at: float, error: str) -> None:
        """持久安排下次重投并保留最近错误摘要。"""
        with self._connection() as connection:
            connection.execute(
                "UPDATE outbox SET available_at=?,last_error=? WHERE outbox_id=?",
                (available_at, error[:300], outbox_id),
            )

    def reject_outbox(self, outbox_id: int, *, error: str) -> None:
        """隔离明确的永久 4xx 协议错误供运维检查。"""
        with self._connection() as connection:
            connection.execute(
                "UPDATE outbox SET state='rejected',last_error=? WHERE outbox_id=?",
                (error[:300], outbox_id),
            )

    def mark_execution_state(self, execution_id: str, generation: int, state: str) -> None:
        """持久改变受控协议执行状态。"""
        with self._connection() as connection:
            result = connection.execute(
                "UPDATE executions SET state=?,updated_at=? "
                "WHERE execution_id=? AND generation=?",
                (state, _utc_now(), execution_id, generation),
            )
            if result.rowcount != 1:
                raise LookupError("worker_execution_unknown")

    def execution_identity(self, execution_id: str, generation: int) -> dict[str, object] | None:
        """查找取消命令绑定的本地 Task、代次和副作用身份。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT task_id,operation_id,cancellation_epoch,state FROM executions "
                "WHERE execution_id=? AND generation=?",
                (execution_id, generation),
            ).fetchone()
            return None if row is None else dict(row)

    def execution_from_outbox(self, dedupe_key: str) -> str:
        """从稳定结果 outbox 键解析 Execution 身份。"""
        parts = dedupe_key.split(":")
        if len(parts) != 3 or parts[0] != "result":
            raise ValueError("worker_outbox_result_key_invalid")
        return parts[1]

    def complete_failure(
        self,
        *,
        execution_id: str,
        generation: int,
        error: dict[str, object],
        result_id: str,
    ) -> None:
        """原子保存 WorkerError 与持续重投的失败结果。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state FROM executions WHERE execution_id=? AND generation=?",
                (execution_id, generation),
            ).fetchone()
            if row is None or row["state"] != "running":
                return
            body = {
                "protocol_version": "1.0",
                "result_id": result_id,
                "generation": generation,
                "status": "failed",
                "error": error,
            }
            now = _utc_now()
            connection.execute(
                "UPDATE executions SET state='failed',result_id=?,updated_at=? "
                "WHERE execution_id=? AND generation=? AND state='running'",
                (result_id, now, execution_id, generation),
            )
            connection.execute(
                "INSERT OR IGNORE INTO outbox(kind,dedupe_key,body_json) VALUES(?,?,?)",
                ("result", f"result:{execution_id}:{generation}", _json(body)),
            )

    def pause_snapshot(self, *, pause_id: str, execution_id: str) -> dict[str, object] | None:
        """读取已持久化并与 Execution 绑定的审批暂停快照。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT p.*,e.task_id,e.operation_id FROM approval_pauses AS p "
                "JOIN executions AS e ON e.execution_id=p.execution_id "
                "AND e.generation=p.generation WHERE p.pause_id=? AND p.execution_id=? "
                "AND p.state='waiting'",
                (pause_id, execution_id),
            ).fetchone()
            return None if row is None else dict(row)

    def save_pause_snapshot(
        self,
        *,
        pause_id: str,
        execution_id: str,
        generation: int,
        cancellation_epoch: int,
        approval_request_id: str,
        approval_batch_id: str | None,
        item_ids: list[str],
        arguments_hashes: list[str],
    ) -> None:
        """保存执行器在发起审批前生成的完整暂停事实。

        参数：
        - `pause_id`（str）：Worker 本地暂停点 UUID。
        - `execution_id`（str）：被暂停执行。
        - `generation`（int）：执行代次。
        - `cancellation_epoch`（int）：中心当前取消栅栏代次。
        - `approval_request_id`（str）：待处理审批请求 UUID。
        - `approval_batch_id`（str | None）：批次请求标识。
        - `item_ids`（list[str]）：原始审批项 ID。
        - `arguments_hashes`（list[str]）：与审批项同序的参数摘要。

        返回：暂停快照和执行状态已在同一事务中持久保存。

        异常：
        - ValueError：暂停项 ID 与参数摘要数量不一致。
        - LookupError：目标执行不存在或已失效。
        """
        if not item_ids or len(item_ids) != len(arguments_hashes):
            raise ValueError("worker_pause_items_invalid")
        with self._connection() as connection:
            execution = connection.execute(
                "SELECT cancellation_epoch,state FROM executions "
                "WHERE execution_id=? AND generation=?",
                (execution_id, generation),
            ).fetchone()
            if (
                execution is None
                or int(execution["cancellation_epoch"]) != cancellation_epoch
                or execution["state"] != "running"
            ):
                raise LookupError("worker_execution_not_running")
            connection.execute(
                "INSERT INTO approval_pauses VALUES(?,?,?,?,?,?,?,?,NULL,'waiting')",
                (
                    pause_id,
                    execution_id,
                    generation,
                    cancellation_epoch,
                    approval_request_id,
                    approval_batch_id,
                    _json(item_ids),
                    _json(arguments_hashes),
                ),
            )
            connection.execute(
                "UPDATE executions SET state='waiting_approval',updated_at=? "
                "WHERE execution_id=? AND generation=?",
                (_utc_now(), execution_id, generation),
            )

    def validate_and_accept_resume(
        self,
        *,
        request: dict[str, object],
        request_hash: str,
        receipt_id: str,
    ) -> StoredReceipt:
        """验证精确审批决定集合并原子收件和关闭暂停点。"""
        command_id = str(request["command_id"])
        with self._connection() as connection:
            previous = connection.execute(
                "SELECT request_hash,receipt_json FROM protocol_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise ValueError("command_id_conflict")
                return StoredReceipt(json.loads(previous["receipt_json"]), True)
            row = connection.execute(
                "SELECT p.item_ids_json,p.arguments_hashes_json,e.task_id,e.operation_id,"
                "e.cancellation_epoch,e.state FROM approval_pauses AS p JOIN executions AS e "
                "ON e.execution_id=p.execution_id AND e.generation=p.generation "
                "WHERE p.pause_id=? AND p.execution_id=? AND p.approval_request_id=? "
                "AND p.state='waiting'",
                (
                    str(request["pause_id"]),
                    str(request["execution_id"]),
                    str(request["approval_request_id"]),
                ),
            ).fetchone()
            if row is None:
                raise LookupError("worker_approval_stale")
            if (
                int(row["cancellation_epoch"]) != int(request["cancellation_epoch"])
                or row["state"] != "waiting_approval"
            ):
                raise LookupError("worker_approval_stale")
            decisions = request["decisions"]
            if not isinstance(decisions, list):
                raise ValueError("worker_approval_decisions_invalid")
            item_ids = json.loads(row["item_ids_json"])
            hashes = json.loads(row["arguments_hashes_json"])
            expected = dict(zip(item_ids, hashes, strict=True))
            actual = {
                str(item["item_id"]): str(item["arguments_sha256"])
                for item in decisions
                if isinstance(item, dict)
            }
            if (
                len(actual) != len(decisions)
                or actual != expected
                or any(not isinstance(item, dict) for item in decisions)
            ):
                raise ValueError("worker_approval_decisions_mismatch")
            receipt = _command_receipt(
                receipt_id,
                command_id,
                str(request["execution_id"]),
                int(request["generation"]),
            )
            connection.execute(
                "INSERT INTO protocol_commands VALUES(?,?,?,?,?,?,?)",
                (
                    command_id,
                    request_hash,
                    "resume",
                    _json(request),
                    _json(receipt),
                    receipt["received_at"],
                    "accepted",
                ),
            )
            connection.execute(
                "UPDATE approval_pauses SET decisions_json=?,state='resumed' WHERE pause_id=?",
                (_json(decisions), str(request["pause_id"])),
            )
            connection.execute(
                "UPDATE executions SET state='queued',updated_at=? "
                "WHERE execution_id=? AND generation=?",
                (_utc_now(), str(request["execution_id"]), int(request["generation"])),
            )
            return StoredReceipt(receipt, False)

    def _enqueue_stop_confirmation(
        self,
        connection: sqlite3.Connection,
        cancel_command: dict[str, object],
        stopped_at: str,
    ) -> None:
        """在 SQLite 同一事务内保存物理停止确认 outbox 项。"""
        from uuid import uuid4

        confirmation = {
            "protocol_version": "1.0",
            "stop_confirmation_id": str(uuid4()),
            "cancel_command_id": cancel_command["command_id"],
            "execution_id": cancel_command["execution_id"],
            "generation": cancel_command["generation"],
            "cancellation_epoch": cancel_command["cancellation_epoch"],
            "reason": cancel_command["reason"],
            "stopped": True,
            "stopped_at": stopped_at,
        }
        connection.execute(
            "INSERT OR IGNORE INTO outbox(kind,dedupe_key,body_json) VALUES(?,?,?)",
            (
                "stop_confirmation",
                f"stop:{confirmation['cancel_command_id']}:{confirmation['execution_id']}:{confirmation['generation']}",
                _json(confirmation),
            ),
        )


__all__ = ["StoredReceipt", "WorkerStore"]

