"""验证 SQLite 按写入热点拆分后的表边界。"""

import sqlite3

import pytest

from athena.infrastructure.postgre.engine import (
    close_sqlite_engines,
    initialize_sqlite_engines,
)


def _tables(path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            )
        }


@pytest.mark.asyncio
async def test_sqlite_tables_are_split_by_write_domain(tmp_path):
    """各数据库只创建所属写入域的表。"""
    paths = {
        name: tmp_path / f"{name}.db"
        for name in ("core", "runtime", "knowledge", "telemetry", "memory")
    }
    await initialize_sqlite_engines(
        str(paths["core"]),
        memory_db_path=str(paths["memory"]),
        runtime_db_path=str(paths["runtime"]),
        knowledge_db_path=str(paths["knowledge"]),
        telemetry_db_path=str(paths["telemetry"]),
    )
    try:
        assert {"sessions", "messages", "tools"}.issubset(_tables(paths["core"]))
        assert "agent_events" not in _tables(paths["core"])
        assert {"agent_events", "stream_snapshots", "tool_calls"}.issubset(
            _tables(paths["runtime"])
        )
        assert {"file_chunks", "file_chunk_fts", "attachments"}.issubset(
            _tables(paths["knowledge"])
        )
        assert {"retrieval_runs", "retrieval_candidates"} == _tables(
            paths["telemetry"]
        )
        assert {"memories", "memory_processing_jobs", "memory_fts"}.issubset(
            _tables(paths["memory"])
        )
    finally:
        await close_sqlite_engines()
