"""Preview or remove knowledge document data without deleting knowledge bases."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOCUMENT_TABLES = (
    "knowledge_jobs",
    "knowledge_requests",
    "knowledge_upload_reservations",
    "knowledge_chunks",
    "knowledge_document_versions",
    "knowledge_documents",
    "knowledge_sources",
)
_PRESERVED_TABLES = ("knowledge_bases", "project_knowledge_bases")
_COUNTED_TABLES = (*_DOCUMENT_TABLES, *_PRESERVED_TABLES, "knowledge_upload_slots")
_LOCK_TIMEOUT = "5s"


def _table(name: str, schema: str | None) -> str:
    if name not in _COUNTED_TABLES:
        raise ValueError("knowledge_reset_table_invalid")
    return name if schema is None else f'"{schema}"."{name}"'


@dataclass(frozen=True, slots=True)
class ResetReport:
    database: str
    before: dict[str, int]
    after: dict[str, int]
    slots_in_use_before: int
    executed: bool


async def run_reset(
    database_url: str,
    *,
    execute: bool,
    schema: str | None = None,
) -> ResetReport:
    """Run one transaction after externally stopping writers and workers."""
    if schema is not None and _SCHEMA.fullmatch(schema) is None:
        raise ValueError("knowledge_reset_schema_invalid")
    connect_args: dict[str, Any] = {"timeout": 8, "command_timeout": 20}
    if schema is not None:
        connect_args["server_settings"] = {
            "search_path": f"{schema},public",
            "statement_timeout": "30000",
            "lock_timeout": "5000",
        }
    engine = create_async_engine(database_url, connect_args=connect_args)
    identity = _database_identity(database_url)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                if execute:
                    await connection.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
                    await connection.execute(
                        text(
                            "LOCK TABLE "
                            + ", ".join(
                                _table(name, schema)
                                for name in (*_DOCUMENT_TABLES, "knowledge_upload_slots")
                            )
                            + " IN ACCESS EXCLUSIVE MODE"
                        )
                    )
                else:
                    await connection.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    )
                before = await _counts(connection, schema)
                slots_before = int(
                    await connection.scalar(
                        text(
                            f"SELECT count(*) FROM {_table('knowledge_upload_slots', schema)} "
                            "WHERE token IS NOT NULL OR lease_until IS NOT NULL"
                        )
                    )
                    or 0
                )
                if execute:
                    for table in _DOCUMENT_TABLES:
                        await connection.execute(text(f"DELETE FROM {_table(table, schema)}"))
                    await connection.execute(
                        text(
                            f"UPDATE {_table('knowledge_upload_slots', schema)} "
                            "SET token = NULL, lease_until = NULL"
                        )
                    )
                after = await _counts(connection, schema)
                if execute:
                    if any(after[table] != 0 for table in _DOCUMENT_TABLES):
                        raise RuntimeError("knowledge_reset_documents_not_empty")
                    if any(after[table] != before[table] for table in _PRESERVED_TABLES):
                        raise RuntimeError("knowledge_reset_preserved_rows_changed")
                    if after["knowledge_upload_slots"] != before["knowledge_upload_slots"]:
                        raise RuntimeError("knowledge_reset_slot_rows_changed")
                    slots_in_use_after = int(
                        await connection.scalar(
                            text(
                                "SELECT count(*) FROM "
                                f"{_table('knowledge_upload_slots', schema)} "
                                "WHERE token IS NOT NULL OR lease_until IS NOT NULL"
                            )
                        )
                        or 0
                    )
                    if slots_in_use_after != 0:
                        raise RuntimeError("knowledge_reset_slots_in_use")
            return ResetReport(
                database=identity,
                before=before,
                after=after,
                slots_in_use_before=slots_before,
                executed=execute,
            )
    finally:
        await engine.dispose()


async def _counts(connection: AsyncConnection, schema: str | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _COUNTED_TABLES:
        counts[table] = int(
            await connection.scalar(text(f"SELECT count(*) FROM {_table(table, schema)}")) or 0
        )
    return counts


def _database_identity(database_url: str) -> str:
    url = make_url(database_url)
    user = url.username or "-"
    host = url.host or "-"
    port = f":{url.port}" if url.port is not None else ""
    database = url.database or "-"
    return f"{url.get_backend_name()}://{user}@{host}{port}/{database}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview by default; pass --execute to delete knowledge document data "
            "while preserving knowledge bases and project bindings."
        )
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("ATHENA_DATABASE_URL"),
        help="Defaults to ATHENA_DATABASE_URL. Prefer the environment variable.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Commit deletions. Without this flag the command is a read-only preview.",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="Optional isolated schema; intended for controlled test environments.",
    )
    return parser


def _print_report(report: ResetReport) -> None:
    mode = "execute" if report.executed else "preview"
    print(f"mode={mode} database={report.database}")
    print(
        "preserved="
        + ", ".join(f"{table}:{report.before.get(table, 0)}" for table in _PRESERVED_TABLES)
    )
    print(f"upload_slots_in_use={report.slots_in_use_before}")
    print(
        "document_data_before="
        + ", ".join(f"{table}:{report.before.get(table, 0)}" for table in _DOCUMENT_TABLES)
    )
    print(
        "document_data_after="
        + ", ".join(f"{table}:{report.after.get(table, 0)}" for table in _DOCUMENT_TABLES)
    )
    if not report.executed:
        print("no_changes=true (pass --execute to commit deletions)")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.database_url:
        print("knowledge_reset_database_required", file=sys.stderr)
        return 2
    try:
        report = asyncio.run(
            run_reset(
                args.database_url,
                execute=bool(args.execute),
                schema=args.schema,
            )
        )
    except Exception as error:
        print(f"knowledge_reset_failed exception_type={type(error).__name__}", file=sys.stderr)
        return 1
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
