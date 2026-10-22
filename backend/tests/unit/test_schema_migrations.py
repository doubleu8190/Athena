"""Versioned PostgreSQL schema migration behavior."""

from __future__ import annotations

import pytest

from infrastructure.persistence.postgres.schema_migrations import apply_schema_migrations


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Connection:
    def __init__(self):
        self.applied: dict[str, str] = {}
        self.migration_sql: list[str] = []
        self.lock_acquired = False

    def exec_driver_sql(self, statement, parameters=None):
        normalized = statement.strip()
        if "pg_advisory_xact_lock" in statement:
            self.lock_acquired = True
            return _Result()
        if "SELECT checksum FROM athena_schema_migrations" in statement:
            return _Result(self.applied.get(parameters[0]))
        if "INSERT INTO athena_schema_migrations" in statement:
            self.applied[parameters[0]] = parameters[1]
            return _Result()
        if normalized.startswith("CREATE TABLE IF NOT EXISTS athena_schema_migrations"):
            return _Result()
        self.migration_sql.append(statement)
        return _Result()


def test_schema_migrations_apply_in_order_once_and_remove_transaction_wrappers(tmp_path):
    (tmp_path / "20261021_second.sql").write_text("BEGIN;\nSELECT 2;\nCOMMIT;\n")
    (tmp_path / "20261020_first.sql").write_text("SELECT 1;\n")
    connection = _Connection()

    apply_schema_migrations(connection, tmp_path)
    apply_schema_migrations(connection, tmp_path)

    assert connection.lock_acquired
    assert connection.migration_sql == ["SELECT 1;", "SELECT 2;"]
    assert list(connection.applied) == ["20261020_first", "20261021_second"]


def test_schema_migration_rejects_changed_applied_file(tmp_path):
    migration = tmp_path / "20261021_example.sql"
    migration.write_text("SELECT 1;")
    connection = _Connection()
    apply_schema_migrations(connection, tmp_path)

    migration.write_text("SELECT 2;")
    with pytest.raises(RuntimeError, match="schema_migration_checksum_mismatch:20261021_example"):
        apply_schema_migrations(connection, tmp_path)
