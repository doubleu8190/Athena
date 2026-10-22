"""Apply versioned PostgreSQL schema migrations in the target adapter."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from sqlalchemy.engine import Connection


MIGRATION_DIRECTORY = Path(__file__).resolve().parents[4] / "sql" / "migrations"


def apply_schema_migrations(
    connection: Connection,
    migration_directory: Path = MIGRATION_DIRECTORY,
) -> None:
    """Apply unapplied SQL files and reject edits to migrations already applied."""

    connection.exec_driver_sql(
        "SELECT pg_advisory_xact_lock(hashtext(current_schema()), "
        "hashtext('athena_schema_migrations'))"
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS athena_schema_migrations (
            version VARCHAR PRIMARY KEY,
            checksum VARCHAR NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    for migration_path in sorted(migration_directory.glob("*.sql")):
        contents = migration_path.read_bytes()
        version = migration_path.stem
        checksum = sha256(contents).hexdigest()
        applied = connection.exec_driver_sql(
            "SELECT checksum FROM athena_schema_migrations WHERE version = %s",
            (version,),
        ).scalar_one_or_none()

        if applied is not None:
            if applied != checksum:
                raise RuntimeError(f"schema_migration_checksum_mismatch:{version}")
            continue

        connection.exec_driver_sql(_without_transaction_wrappers(contents.decode("utf-8")))
        connection.exec_driver_sql(
            "INSERT INTO athena_schema_migrations (version, checksum) VALUES (%s, %s)",
            (version, checksum),
        )


def _without_transaction_wrappers(sql: str) -> str:
    """Keep migration files manually runnable while letting the caller transact."""

    return "\n".join(
        line
        for line in sql.splitlines()
        if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
    )


__all__ = ["MIGRATION_DIRECTORY", "apply_schema_migrations"]
