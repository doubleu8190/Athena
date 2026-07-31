"""SQLite 数据库管理 — 基于 aiosqlite 的异步访问层."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import aiosqlite

from athena.db.schema import get_schema
from athena.utils.logging import get_logger

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now().isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class Database:
    """异步数据库访问层.

    封装 sessions / messages / steps / tool_call / approval_logs 的 CRUD 操作。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """建立连接并初始化表结构."""
        import os
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)

        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(get_schema())
        await self._conn.commit()
        logger.info("database_connected", db_path=self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("database_closed")

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def create_session(self, session_id: str, title: str = "New Session") -> dict[str, Any]:
        now = _now_iso()
        await self.conn.execute(
            "INSERT INTO sessions (id, title, status, created_at, updated_at, metadata) "
            "VALUES (?, ?, 'idle', ?, ?, '{}')",
            (session_id, title, now, now),
        )
        await self.conn.commit()
        return {"id": session_id, "title": title, "status": "idle", "created_at": now, "updated_at": now}

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            d = dict(row)
            d["metadata"] = _json_loads(d.get("metadata"), {})
            return d

    async def list_sessions(self) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["metadata"] = _json_loads(d.get("metadata"), {})
                result.append(d)
            return result

    async def update_session(
        self, session_id: str, *, status: str | None = None, run_id: str | None = None,
        title: str | None = None,
    ) -> None:
        fields: list[str] = []
        params: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if run_id is not None:
            fields.append("run_id = ?")
            params.append(run_id)
        if title is not None:
            fields.append("title = ?")
            params.append(title)
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(_now_iso())
        params.append(session_id)
        await self.conn.execute(
            f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?", params
        )
        await self.conn.commit()

    async def query_sessions(self, status: list[str]) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(status))
        async with self.conn.execute(
            f"SELECT * FROM sessions WHERE status IN ({placeholders})", status
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def delete_session(self, session_id: str) -> None:
        await self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self.conn.execute("DELETE FROM tool_call WHERE session_id = ?", (session_id,))
        await self.conn.execute("DELETE FROM steps WHERE session_id = ?", (session_id,))
        await self.conn.execute("DELETE FROM approval_logs WHERE session_id = ?", (session_id,))
        await self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def save_message(self, session_id: str, message: dict[str, Any]) -> str:
        msg_id = message.get("id") or str(uuid.uuid4())
        await self.conn.execute(
            "INSERT INTO messages (id, session_id, role, content, tool_calls, tool_call_id, metadata, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                session_id,
                message["role"],
                message.get("content", ""),
                _json_dumps(message.get("tool_calls", [])),
                message.get("tool_call_id"),
                _json_dumps(message.get("metadata", {})),
                message.get("timestamp", _now_iso()),
            ),
        )
        await self.conn.commit()
        await self.update_session(session_id)  # refresh updated_at
        return msg_id

    async def get_messages(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp ASC"
        params: list[Any] = [session_id]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["tool_calls"] = _json_loads(d.get("tool_calls"), [])
                d["metadata"] = _json_loads(d.get("metadata"), {})
                result.append(d)
            return result

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    async def save_step(self, step: dict[str, Any]) -> None:
        await self.conn.execute(
            "INSERT INTO steps (id, session_id, run_id, step_number, step_type, parent_step_id, "
            "status, started_at, completed_at, duration_ms, llm_input_tokens, llm_output_tokens, "
            "error_message, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                step["id"],
                step["session_id"],
                step["run_id"],
                step["step_number"],
                step["step_type"],
                step.get("parent_step_id"),
                step.get("status", "pending"),
                step["started_at"],
                step.get("completed_at"),
                step.get("duration_ms", 0),
                step.get("llm_input_tokens", 0),
                step.get("llm_output_tokens", 0),
                step.get("error_message"),
                _json_dumps(step.get("metadata", {})),
            ),
        )
        await self.conn.commit()

    async def update_step(self, step_id: str, updates: dict[str, Any]) -> None:
        if not updates:
            return
        allowed = {
            "status", "completed_at", "duration_ms", "llm_input_tokens",
            "llm_output_tokens", "error_message", "metadata",
        }
        fields: list[str] = []
        params: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            if key == "metadata":
                value = _json_dumps(value)
            fields.append(f"{key} = ?")
            params.append(value)
        if not fields:
            return
        params.append(step_id)
        await self.conn.execute(
            f"UPDATE steps SET {', '.join(fields)} WHERE id = ?", params
        )
        await self.conn.commit()

    async def get_steps(self, session_id: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM steps WHERE session_id = ? ORDER BY step_number ASC", (session_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["metadata"] = _json_loads(d.get("metadata"), {})
                result.append(d)
            return result

    async def get_steps_by_run(self, run_id: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY step_number ASC", (run_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_last_step_number(self, run_id: str) -> int:
        async with self.conn.execute(
            "SELECT MAX(step_number) as max_num FROM steps WHERE run_id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row["max_num"] is not None:
                return row["max_num"]
            return 0

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    async def save_tool_call(self, tool_call: dict[str, Any]) -> None:
        await self.conn.execute(
            "INSERT INTO tool_call (id, session_id, step_id, tool_name, arguments, raw_output, "
            "status, started_at, completed_at, duration_ms, error_message, error_stack) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tool_call["id"],
                tool_call["session_id"],
                tool_call["step_id"],
                tool_call["tool_name"],
                _json_dumps(tool_call.get("arguments", {})),
                tool_call.get("raw_output"),
                tool_call.get("status", "pending"),
                tool_call["started_at"],
                tool_call.get("completed_at"),
                tool_call.get("duration_ms", 0),
                tool_call.get("error_message"),
                tool_call.get("error_stack"),
            ),
        )
        await self.conn.commit()

    async def update_tool_call(self, tool_call_id: str, updates: dict[str, Any]) -> None:
        if not updates:
            return
        allowed = {
            "raw_output", "status", "completed_at", "duration_ms",
            "error_message", "error_stack",
        }
        fields: list[str] = []
        params: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            fields.append(f"{key} = ?")
            params.append(value)
        if not fields:
            return
        params.append(tool_call_id)
        await self.conn.execute(
            f"UPDATE tool_call SET {', '.join(fields)} WHERE id = ?", params
        )
        await self.conn.commit()

    async def query_tool_calls(
        self, session_id: str, status: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tool_call WHERE session_id = ?"
        params: list[Any] = [session_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY started_at ASC"
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["arguments"] = _json_loads(d.get("arguments"), {})
                result.append(d)
            return result

    # ------------------------------------------------------------------
    # Approval logs
    # ------------------------------------------------------------------

    async def save_approval_log(self, log: dict[str, Any]) -> None:
        await self.conn.execute(
            "INSERT INTO approval_logs (id, session_id, tool_call_id, tool_name, arguments, "
            "risk_level, decision, decision_time_ms, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                log["id"],
                log["session_id"],
                log["tool_call_id"],
                log["tool_name"],
                _json_dumps(log.get("arguments", {})),
                log["risk_level"],
                log["decision"],
                log.get("decision_time_ms", 0),
                log["timestamp"],
            ),
        )
        await self.conn.commit()

    async def get_approval_logs(self, session_id: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM approval_logs WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["arguments"] = _json_loads(d.get("arguments"), {})
                result.append(d)
            return result

    async def query_approval(self, tool_call_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM approval_logs WHERE tool_call_id = ? LIMIT 1", (tool_call_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_db_instance: Database | None = None


async def get_database(db_path: str) -> Database:
    """获取数据库单例."""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database(db_path)
        await _db_instance.connect()
    return _db_instance


async def close_database() -> None:
    global _db_instance
    if _db_instance is not None:
        await _db_instance.close()
        _db_instance = None
