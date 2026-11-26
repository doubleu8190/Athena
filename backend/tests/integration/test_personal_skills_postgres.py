"""个人 Skill 的真实 PostgreSQL 迁移、回填与范围唯一约束测试。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from typing import Any

import pytest
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="ATHENA_TEST_DATABASE_URL is not configured",
    ),
]


def test_personal_skill_migration_backfill_and_scoped_constraints() -> None:
    """旧公共记录回填、跨用户同内容、slug 范围和软删除复用均由真实 PG 验证。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    try:
        asyncio.run(_run_personal_skill_migration_scenario(database_url))
    except Exception as error:
        # 当前环境允许配置测试库地址但 PostgreSQL 尚未启动；连接拒绝属于
        # 环境不可用，应保留该集成测试供数据库恢复后直接执行。
        if _is_connection_refused(error):
            pytest.skip("PostgreSQL unavailable: connection refused")
        raise


async def _run_personal_skill_migration_scenario(database_url: str) -> None:
    """在随机 schema 中执行基线迁移并验证企业与个人 Skill 唯一索引及检查约束。"""
    async with isolated_postgres_schema(
        database_url,
        prefix="athena_v2_personal_skills",
    ) as db:
        test_engine = db.engine
        await _seed_users_and_company_skill(test_engine)

        async with test_engine.connect() as connection:
            seeded = (
                await connection.execute(
                    text(
                        "SELECT scope, owner_id, deleted_at "
                        "FROM skills WHERE id = 'skill_company_seeded'"
                    )
                )
            ).one()
        assert seeded.scope == "company"
        assert seeded.owner_id is None
        assert seeded.deleted_at is None

        await _insert_personal_skill(
            test_engine,
            skill_id="skill_personal_a",
            version_id="skillver_personal_a",
            owner_id="usr_personal_a",
            slug="shared-scope-slug",
            package_sha256="a" * 64,
            object_key="skills/aa/personal-a.zip",
        )
        await _insert_personal_skill(
            test_engine,
            skill_id="skill_personal_b",
            version_id="skillver_personal_b",
            owner_id="usr_personal_b",
            slug="shared-scope-slug",
            package_sha256="a" * 64,
            object_key="skills/aa/personal-b.zip",
        )

        await _expect_integrity_error(
            test_engine,
            text(
                "INSERT INTO skills "
                "(id, slug, scope, owner_id, deleted_at, enabled, "
                "script_requires_approval, revision, created_by, created_at, updated_at) "
                "VALUES ('skill_public_duplicate', 'shared-scope-slug', 'company', NULL, NULL, "
                "false, false, 1, NULL, :now, :now)"
            ),
            {"now": datetime.now(UTC)},
        )
        await _expect_integrity_error(
            test_engine,
            text(
                "INSERT INTO skills "
                "(id, slug, scope, owner_id, deleted_at, enabled, "
                "script_requires_approval, format, featured, revision, "
                "created_by, created_at, updated_at) "
                "VALUES ('skill_personal_duplicate', 'shared-scope-slug', 'personal', "
                "'usr_personal_a', NULL, false, false, 'markdown', false, 1, "
                "'usr_personal_a', :now, :now)"
            ),
            {"now": datetime.now(UTC)},
        )
        await _expect_integrity_error(
            test_engine,
            text(
                "INSERT INTO skills "
                "(id, slug, scope, owner_id, deleted_at, enabled, "
                "script_requires_approval, revision, created_by, created_at, updated_at) "
                "VALUES ('skill_personal_invalid', 'ownerless', 'personal', NULL, NULL, "
                "false, false, 1, NULL, :now, :now)"
            ),
            {"now": datetime.now(UTC)},
        )

        async with test_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE skills SET deleted_at = :now, updated_at = :now "
                    "WHERE id = 'skill_personal_a'"
                ),
                {"now": datetime.now(UTC)},
            )
        await _insert_personal_skill(
            test_engine,
            skill_id="skill_personal_a_recreated",
            version_id="skillver_personal_a_recreated",
            owner_id="usr_personal_a",
            slug="shared-scope-slug",
            package_sha256="a" * 64,
            object_key="skills/aa/personal-a-recreated.zip",
        )

        async with test_engine.connect() as connection:
            index_rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT "
                            "index_class.relname AS index_name, "
                            "index_row.indisunique AS is_unique, "
                            "pg_get_expr(index_row.indpred, index_row.indrelid) "
                            "AS predicate, "
                            "ARRAY("
                            "SELECT attribute.attname "
                            "FROM unnest(index_row.indkey) "
                            "WITH ORDINALITY AS key(attnum, ordinal) "
                            "JOIN pg_attribute AS attribute "
                            "ON attribute.attrelid = index_row.indrelid "
                            "AND attribute.attnum = key.attnum "
                            "ORDER BY key.ordinal"
                            ") AS columns "
                            "FROM pg_index AS index_row "
                            "JOIN pg_class AS table_row "
                            "ON table_row.oid = index_row.indrelid "
                            "JOIN pg_class AS index_class "
                            "ON index_class.oid = index_row.indexrelid "
                            "JOIN pg_namespace AS namespace "
                            "ON namespace.oid = table_row.relnamespace "
                            "WHERE namespace.nspname = current_schema() "
                            "AND table_row.relname IN ('skills', 'skill_versions') "
                            "AND index_class.relname IN ("
                            "'uq_skills_company_slug', "
                            "'uq_skills_personal_owner_slug', "
                            "'ix_skill_versions_package_sha256')"
                        )
                    )
                )
                .mappings()
                .all()
            )
            constraint_names = set(
                (
                    await connection.scalars(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'skills'::regclass "
                            "AND conname IN ('ck_skills_scope', 'ck_skills_owner_scope')"
                        )
                    )
                ).all()
            )
        indexes = {row["index_name"]: row for row in index_rows}
        company_slug = indexes["uq_skills_company_slug"]
        personal_owner_slug = indexes["uq_skills_personal_owner_slug"]
        package_hash = indexes["ix_skill_versions_package_sha256"]
        assert company_slug["is_unique"] is True
        assert company_slug["columns"] == ["slug"]
        assert "scope" in company_slug["predicate"]
        assert "company" in company_slug["predicate"]
        assert personal_owner_slug["is_unique"] is True
        assert personal_owner_slug["columns"] == ["owner_id", "slug"]
        assert "scope" in personal_owner_slug["predicate"]
        assert "personal" in personal_owner_slug["predicate"]
        assert "deleted_at" in personal_owner_slug["predicate"]
        assert "IS NULL" in personal_owner_slug["predicate"]
        assert package_hash["is_unique"] is False
        assert package_hash["columns"] == ["package_sha256"]
        assert constraint_names == {"ck_skills_scope", "ck_skills_owner_scope"}


def _is_connection_refused(error: BaseException) -> bool:
    """识别数据库连接拒绝，并避免在 skip 原因中回显测试库 URL。"""
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        if "connection refused" in str(current).casefold():
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


async def _seed_users_and_company_skill(test_engine: Any) -> None:
    """写入两个普通用户和一条企业级 Skill，验证同 slug 在不同 scope 下的约束边界。"""
    now = datetime.now(UTC)
    async with test_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users "
                "(id, email, password_hash, display_name, status, role, "
                "created_at, updated_at, deleted_time) VALUES "
                "('usr_personal_a', 'a@example.test', 'unused', NULL, 'active', 'member', "
                ":now, :now, NULL), "
                "('usr_personal_b', 'b@example.test', 'unused', NULL, 'active', 'member', "
                ":now, :now, NULL)"
            ),
            {"now": now},
        )
        await connection.execute(
            text(
                "INSERT INTO skills "
                "(id, slug, scope, current_version_id, enabled, script_requires_approval, "
                "revision, created_by, created_at, updated_at) "
                "VALUES ('skill_company_seeded', 'shared-scope-slug', 'company', NULL, false, "
                "false, 1, NULL, :now, :now)"
            ),
            {"now": now},
        )


async def _insert_personal_skill(
    test_engine: Any,
    *,
    skill_id: str,
    version_id: str,
    owner_id: str,
    slug: str,
    package_sha256: str,
    object_key: str,
) -> None:
    """写入一个当前启用的个人身份和同内容版本。"""
    now = datetime.now(UTC)
    manifest = {
        "version": 1,
        "slug": slug,
        "name": "个人 Skill",
        "description": "迁移约束测试",
        "dependencies": [],
        "guide": "正文",
        "scripts": [],
        "files": [
            {
                "path": "SKILL.md",
                "size_bytes": 6,
                "sha256": "b" * 64,
                "text": True,
            }
        ],
    }
    dependency_check = {
        "status": "satisfied",
        "python_version": "not-checked",
        "python_implementation": "markdown-only",
        "items": [],
        "checked_at": now.isoformat(),
    }
    async with test_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO skills "
                "(id, slug, scope, owner_id, deleted_at, current_version_id, enabled, "
                "script_requires_approval, format, featured, revision, "
                "created_by, created_at, updated_at) "
                "VALUES (:skill_id, :slug, 'personal', :owner_id, NULL, NULL, true, false, "
                "'markdown', false, 1, :owner_id, :now, :now)"
            ),
            {
                "skill_id": skill_id,
                "slug": slug,
                "owner_id": owner_id,
                "now": now,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO skill_versions "
                "(id, skill_id, ordinal, label, guide, manifest, object_key, "
                "package_sha256, dependency_requirements, dependency_check, created_by, "
                "request_operation, idempotency_key, request_fingerprint, "
                "request_target_skill_id, created_at) "
                "VALUES (:version_id, :skill_id, 1, 'v1', '正文', CAST(:manifest AS jsonb), "
                ":object_key, :package_sha256, CAST('[]' AS jsonb), "
                "CAST(:dependency_check AS jsonb), :owner_id, 'personal_create', :key, "
                ":fingerprint, NULL, :now)"
            ),
            {
                "version_id": version_id,
                "skill_id": skill_id,
                "manifest": json.dumps(manifest, ensure_ascii=False),
                "object_key": object_key,
                "package_sha256": package_sha256,
                "dependency_check": json.dumps(dependency_check),
                "owner_id": owner_id,
                "key": f"create-{skill_id}",
                "fingerprint": "c" * 64,
                "now": now,
            },
        )
        await connection.execute(
            text("UPDATE skills SET current_version_id = :version_id WHERE id = :skill_id"),
            {"version_id": version_id, "skill_id": skill_id},
        )


async def _expect_integrity_error(
    test_engine: Any,
    statement: Any,
    parameters: dict[str, Any],
) -> None:
    """执行预期违反数据库约束的独立事务。"""
    with pytest.raises(IntegrityError):
        async with test_engine.begin() as connection:
            await connection.execute(statement, parameters)
