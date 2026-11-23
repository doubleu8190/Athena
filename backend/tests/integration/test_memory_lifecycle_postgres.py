"""0027 记忆决策 schema 与历史 JSON 投影的 PostgreSQL 验证。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from integration.test_memory_lifecycle_support import (
    DATABASE_URL,
    HeadFixture,
    isolated_database,
    seed_head_fixture,
)
from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.memory.model import MemoryModel, MemorySourceModel
from adapter.memory.repository import MemoryRepository
from adapter.message.model import ConversationMessageModel
from domain.memory.lifecycle import ExtractJobResult
from domain.memory.model import AccessContext
from domain.system_prompt import INITIAL_SYSTEM_PROMPT_IDS

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="test DB not configured"),
]

_FACT_COLUMNS = {
    "subject_key",
    "fact_key",
    "fact_value",
    "cardinality",
    "polarity",
    "applicability",
    "applicability_key",
    "normalization_version",
}


async def _seed_0026_fixture(
    factory: async_sessionmaker[AsyncSession],
    *,
    owner_id: str,
    generation: int,
) -> HeadFixture:
    """在 20261125_0026 历史 schema 下写入基础数据与显式 0026 列名 Run。"""
    base = await seed_head_fixture(
        factory,
        owner_id=owner_id,
        pending_count=0,
        generation=generation,
    )
    run_id = f"run_{owner_id}_000"
    message_id = f"message_{owner_id}_000"
    source_at = base.now + timedelta(microseconds=1)
    async with factory() as session, session.begin():
        message = ConversationMessageModel(
            id=message_id,
            session_id=base.session_id,
            role="user",
            status="completed",
            sequence=1,
            parts=[{"type": "text", "text": "用户证据 1"}],
            reply_to_message_id=None,
            run_id=None,
            managed_run_id=None,
            context_turn_id=None,
            client_message_id=None,
            created_at=source_at,
            completed_at=source_at,
            metadata_json={},
        )
        session.add(message)
        await session.flush()
        # 显式使用 0026 时代 runs 表列名插入，避免 head ORM 的 subagent 字段导致 UndefinedColumn。
        await session.execute(
            text(
                "INSERT INTO runs ("
                "id, session_id, owner_id, status, trigger_message_id, context_cutoff_sequence, "
                "retry_of_run_id, model_config_version_id, main_prompt_version_id, "
                "compression_prompt_version_id, compression_policy_version_id, skill_snapshot, "
                "input_file_ids, memory_project_id, memory_input_message_ids, "
                "memory_context_state, "
                "memory_extraction_status, memory_extracted_at, memory_extraction_batch_id, "
                "memory_extraction_safe_error_code, tool_snapshot, auth_session_id, "
                "llm_request_count, prompt_template_version, event_sequence, execution_epoch, "
                "worker_id, lease_expires_at, execution_started_at, execution_deadline_at, "
                "queued_at, first_started_at, finished_at, created_at, updated_at, "
                "cancel_requested_at, safe_error_code, report_message_id, usage"
                ") VALUES ("
                ":id, :session_id, :owner_id, 'completed', :trigger_message_id, 1, "
                "NULL, :model_config_version_id, :main_prompt_version_id, "
                ":compression_prompt_version_id, NULL, NULL, "
                "CAST('[]' AS jsonb), :memory_project_id, "
                "CAST(:memory_input_message_ids AS jsonb), "
                "CAST(:memory_context_state AS jsonb), "
                "'pending', NULL, NULL, NULL, NULL, :auth_session_id, "
                "0, 'run-v2', 0, 0, NULL, NULL, NULL, NULL, "
                ":source_at, NULL, :source_at, :source_at, :source_at, "
                "NULL, NULL, NULL, CAST('{}' AS jsonb)"
                ")"
            ),
            {
                "id": run_id,
                "session_id": base.session_id,
                "owner_id": owner_id,
                "trigger_message_id": message_id,
                "model_config_version_id": base.profile_id,
                "main_prompt_version_id": INITIAL_SYSTEM_PROMPT_IDS["main"],
                "compression_prompt_version_id": INITIAL_SYSTEM_PROMPT_IDS["compression"],
                "memory_project_id": base.project_id,
                "memory_input_message_ids": json.dumps([message_id]),
                "memory_context_state": json.dumps(
                    {
                        "initialized": True,
                        "references": [
                            {
                                "memory_id": f"memory_{owner_id}",
                                "version": 1,
                                "scope_key": "personal",
                            }
                        ],
                    }
                ),
                "auth_session_id": base.auth_session_id,
                "source_at": source_at,
            },
        )
        await session.execute(
            text("UPDATE sessions SET last_message_sequence = 1 WHERE id = :session_id"),
            {"session_id": base.session_id},
        )
        message.run_id = run_id
    return replace(base, run_ids=(run_id,), message_ids=(message_id,))


def test_0027_preserves_data_projects_extract_json_and_drops_fact_schema() -> None:
    """从 0026 升级后只删除事实身份结构，并保留审计与完成语义。"""

    async def scenario() -> None:
        async with isolated_database(revision="20261125_0026") as harness:
            fixture = await _seed_0026_fixture(
                harness.factory,
                owner_id="owner_0027",
                generation=9,
            )
            context_state = {
                "initialized": True,
                "references": [
                    {
                        "memory_id": "memory_0027_conditional",
                        "version": 1,
                        "scope_key": "personal",
                    }
                ],
            }
            old_extract_result = {
                "kind": "extract_pending",
                "result": {
                    "batch_id": "batch_old",
                    "run_ids": [fixture.run_ids[0]],
                    "created": 1,
                    "merged": 1,
                    "replaced": 2,
                    "skipped": 4,
                    "status": "applied",
                    "discard_reason": None,
                },
            }
            control_payload = {
                "kind": "control",
                "owner_id": fixture.owner_id,
                "run_id": fixture.run_ids[0],
                "tool_call_id": "call_control",
                "operation": "forget",
                "arguments_digest": "a" * 64,
                "auth_session_id": fixture.auth_session_id,
                "namespace_id": fixture.owner_id,
                "scope_key": "personal",
            }
            control_result = {
                "kind": "control",
                "result": {
                    "operation": "forget",
                    "memory_id": "memory_0027",
                    "generation": 9,
                },
            }
            conditional_content = "周五推荐水果时优先考虑梨"
            negated_content = "用户不吃香菜"
            plain_content = "用户偏好使用中文"

            async with harness.factory() as session, session.begin():
                session.add(
                    MemoryModel(
                        id="memory_0027",
                        namespace_id=fixture.owner_id,
                        scope_key="personal",
                        content="升级后必须保留的正文",
                        search_document=func.to_tsvector(
                            "athena_memory_chinese",
                            "升级后必须保留的正文",
                        ),
                        embedding=None,
                        embedding_profile_id="bge-m3-1024-v1",
                        request_id="request_0027",
                        request_hash="b" * 64,
                        status="active",
                        origin="explicit",
                        version=1,
                        expires_at=None,
                        kind="preference",
                        load_policy="on_demand",
                        content_hash="c" * 64,
                        explicit_long_term=True,
                        last_evidenced_at=fixture.now,
                        superseded_by_id=None,
                        created_at=fixture.now,
                        updated_at=fixture.now,
                        deleted_time=None,
                    )
                )
                session.add_all(
                    [
                        MemoryModel(
                            id="memory_0027_conditional",
                            namespace_id=fixture.owner_id,
                            scope_key="personal",
                            content=conditional_content,
                            search_document=func.to_tsvector(
                                "athena_memory_chinese",
                                conditional_content,
                            ),
                            embedding=[0.2] * 1024,
                            embedding_profile_id="bge-m3-1024-v1",
                            request_id="request_0027_conditional",
                            request_hash="1" * 64,
                            status="active",
                            origin="extracted",
                            version=1,
                            expires_at=None,
                            kind="preference",
                            load_policy="on_demand",
                            content_hash="2" * 64,
                            explicit_long_term=False,
                            last_evidenced_at=fixture.now,
                            superseded_by_id=None,
                            created_at=fixture.now,
                            updated_at=fixture.now,
                            deleted_time=None,
                        ),
                        MemoryModel(
                            id="memory_0027_negated",
                            namespace_id=fixture.owner_id,
                            scope_key="personal",
                            content=negated_content,
                            search_document=func.to_tsvector(
                                "athena_memory_chinese",
                                negated_content,
                            ),
                            embedding=[0.3] * 1024,
                            embedding_profile_id="bge-m3-1024-v1",
                            request_id="request_0027_negated",
                            request_hash="3" * 64,
                            status="active",
                            origin="extracted",
                            version=1,
                            expires_at=None,
                            kind="fact",
                            load_policy="on_demand",
                            content_hash="4" * 64,
                            explicit_long_term=False,
                            last_evidenced_at=fixture.now,
                            superseded_by_id=None,
                            created_at=fixture.now,
                            updated_at=fixture.now,
                            deleted_time=None,
                        ),
                        MemoryModel(
                            id="memory_0027_plain",
                            namespace_id=fixture.owner_id,
                            scope_key="personal",
                            content=plain_content,
                            search_document=func.to_tsvector(
                                "athena_memory_chinese",
                                plain_content,
                            ),
                            embedding=[0.4] * 1024,
                            embedding_profile_id="bge-m3-1024-v1",
                            request_id="request_0027_plain",
                            request_hash="5" * 64,
                            status="active",
                            origin="explicit",
                            version=1,
                            expires_at=None,
                            kind="preference",
                            load_policy="on_demand",
                            content_hash="6" * 64,
                            explicit_long_term=True,
                            last_evidenced_at=fixture.now,
                            superseded_by_id=None,
                            created_at=fixture.now,
                            updated_at=fixture.now,
                            deleted_time=None,
                        ),
                        MemoryModel(
                            id="memory_0027_deleted",
                            namespace_id=fixture.owner_id,
                            scope_key="personal",
                            content="",
                            search_document=func.to_tsvector(
                                "athena_memory_chinese",
                                "",
                            ),
                            embedding=None,
                            embedding_profile_id=None,
                            request_id="request_0027_deleted",
                            request_hash="7" * 64,
                            status="archived",
                            origin=None,
                            version=2,
                            expires_at=None,
                            kind=None,
                            load_policy=None,
                            content_hash=None,
                            explicit_long_term=None,
                            last_evidenced_at=None,
                            superseded_by_id=None,
                            created_at=fixture.now,
                            updated_at=fixture.now,
                            deleted_time=fixture.now,
                        ),
                    ]
                )
                session.add(
                    MemorySourceModel(
                        id="source_0027",
                        memory_id="memory_0027",
                        source_kind="message",
                        source_ref=fixture.message_ids[0],
                        source_at=fixture.now,
                    )
                )
                await session.flush()
                await session.execute(
                    text(
                        "UPDATE memories SET "
                        "subject_key = 'owner', fact_key = 'liked_fruit', "
                        "fact_value = 'pear', cardinality = 'single', "
                        "polarity = 'affirmed', "
                        'applicability = \'{"topic":"","conditions":[]}\'::jsonb, '
                        "applicability_key = :applicability_key, "
                        "normalization_version = 1 "
                        "WHERE id = 'memory_0027'"
                    ),
                    {"applicability_key": "d" * 64},
                )
                structural_values: tuple[tuple[str, str, dict[str, object]], ...] = (
                    (
                        "memory_0027_conditional",
                        "affirmed",
                        {
                            "topic": "",
                            "conditions": ["推荐水果时"],
                            "valid_from": "2026-11-20T00:00:00+00:00",
                            "valid_to": "2026-11-30T23:59:59+00:00",
                        },
                    ),
                    (
                        "memory_0027_negated",
                        "negated",
                        {
                            "topic": "",
                            "conditions": [],
                            "valid_from": None,
                            "valid_to": None,
                        },
                    ),
                    (
                        "memory_0027_plain",
                        "affirmed",
                        {
                            "topic": "",
                            "conditions": [],
                            "valid_from": None,
                            "valid_to": None,
                        },
                    ),
                    (
                        "memory_0027_deleted",
                        "negated",
                        {
                            "topic": "",
                            "conditions": ["仅在周末"],
                            "valid_from": None,
                            "valid_to": None,
                        },
                    ),
                )
                for memory_id, polarity, applicability in structural_values:
                    await session.execute(
                        text(
                            "UPDATE memories SET polarity = :polarity, "
                            "applicability = CAST(:applicability AS jsonb) "
                            "WHERE id = :memory_id"
                        ),
                        {
                            "memory_id": memory_id,
                            "polarity": polarity,
                            "applicability": json.dumps(applicability),
                        },
                    )
                await session.execute(
                    text(
                        "UPDATE runs SET memory_context_state = CAST(:context AS jsonb) "
                        "WHERE id = :run_id"
                    ),
                    {
                        "context": json.dumps(context_state),
                        "run_id": fixture.run_ids[0],
                    },
                )
                for status in ("done", "queued", "running", "failed"):
                    dedupe_key = f"extract_pending:{fixture.owner_id}:{status}"
                    await session.execute(
                        text(
                            "INSERT INTO memory_jobs ("
                            "id, dedupe_key, kind, status, payload, result, namespace_id, "
                            "scope_key, attempt_count, schema_retry_count, available_at, "
                            "created_at, updated_at"
                            ") VALUES ("
                            ":id, :dedupe_key, 'extract_pending', :status, "
                            "CAST(:payload AS jsonb), CAST(:result AS jsonb), :owner_id, "
                            "NULL, 1, 0, :now, :now, :now"
                            ")"
                        ),
                        {
                            "id": f"job_extract_{status}",
                            "dedupe_key": dedupe_key,
                            "status": status,
                            "payload": json.dumps(
                                {
                                    "kind": "extract_pending",
                                    "owner_id": fixture.owner_id,
                                }
                            ),
                            "result": json.dumps(old_extract_result),
                            "owner_id": fixture.owner_id,
                            "now": fixture.now,
                        },
                    )
                await session.execute(
                    text(
                        "INSERT INTO memory_jobs ("
                        "id, dedupe_key, kind, status, payload, result, namespace_id, "
                        "scope_key, attempt_count, schema_retry_count, available_at, "
                        "created_at, updated_at"
                        ") VALUES ("
                        "'job_control', 'control:run:call', 'control', 'done', "
                        "CAST(:payload AS jsonb), CAST(:result AS jsonb), :owner_id, "
                        "'personal', 0, 0, :now, :now, :now"
                        ")"
                    ),
                    {
                        "payload": json.dumps(control_payload),
                        "result": json.dumps(control_result),
                        "owner_id": fixture.owner_id,
                        "now": fixture.now,
                    },
                )

            # 精确升级至目标迁移 20261126_0027，避免后续新迁移改变 revision 断言与回滚路径。
            await harness.upgrade_to("20261126_0027")

            async with harness.factory() as session:
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
                columns = set(
                    (
                        await session.scalars(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = current_schema() "
                                "AND table_name = 'memories'"
                            )
                        )
                    ).all()
                )
                index_exists = await session.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                        "WHERE schemaname = current_schema() "
                        "AND indexname = 'ix_memories_fact_group')"
                    )
                )
                constraints = set(
                    (
                        await session.scalars(
                            text(
                                "SELECT conname FROM pg_constraint "
                                "WHERE conrelid = 'memories'::regclass"
                            )
                        )
                    ).all()
                )
                memory = (
                    (
                        await session.execute(
                            text(
                                "SELECT content, superseded_by_id FROM memories "
                                "WHERE id = 'memory_0027'"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                preserved_rows = {
                    row["id"]: row
                    for row in (
                        await session.execute(
                            text(
                                "SELECT id, content, content_hash, embedding IS NULL "
                                "AS embedding_is_null, embedding_profile_id, "
                                "version, updated_at, "
                                "search_document = to_tsvector("
                                "'athena_memory_chinese', content"
                                ") AS search_document_matches "
                                "FROM memories WHERE id IN ("
                                "'memory_0027_conditional', 'memory_0027_negated', "
                                "'memory_0027_plain', 'memory_0027_deleted'"
                                ")"
                            )
                        )
                    ).mappings()
                }
                source = await session.scalar(
                    text("SELECT source_ref FROM memory_sources WHERE memory_id = 'memory_0027'")
                )
                generation = await session.scalar(
                    text("SELECT generation FROM memory_settings WHERE namespace_id = :owner_id"),
                    {"owner_id": fixture.owner_id},
                )
                run = (
                    (
                        await session.execute(
                            text(
                                "SELECT CAST(memory_context_state AS text) AS context "
                                "FROM runs WHERE id = :run_id"
                            ),
                            {"run_id": fixture.run_ids[0]},
                        )
                    )
                    .mappings()
                    .one()
                )
                jobs = {
                    row["id"]: row
                    for row in (
                        await session.execute(
                            text(
                                "SELECT id, status, CAST(result AS text) AS result "
                                "FROM memory_jobs WHERE kind = 'extract_pending'"
                            )
                        )
                    ).mappings()
                }
                control = (
                    (
                        await session.execute(
                            text(
                                "SELECT CAST(payload AS text) AS payload, "
                                "CAST(result AS text) AS result FROM memory_jobs "
                                "WHERE id = 'job_control'"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )

            assert revision == "20261126_0027"
            assert _FACT_COLUMNS.isdisjoint(columns)
            assert not index_exists
            assert {"ck_memories_cardinality", "ck_memories_polarity"}.isdisjoint(constraints)
            assert memory["content"] == "升级后必须保留的正文"
            assert memory["superseded_by_id"] is None
            assert source == fixture.message_ids[0]
            assert generation == 9
            assert json.loads(run["context"]) == context_state
            assert json.loads(control["payload"]) == control_payload
            migrated_control_payload = json.loads(control["payload"])
            assert migrated_control_payload["arguments_digest"] == "a" * 64
            assert json.loads(control["result"]) == control_result
            conditional = preserved_rows["memory_0027_conditional"]
            assert conditional["content"].startswith(conditional_content + "\n\n历史限定")
            assert "推荐水果时" in conditional["content"]
            assert "2026-11-20T00:00:00+00:00" in conditional["content"]
            assert "2026-11-30T23:59:59+00:00" in conditional["content"]
            assert (
                conditional["content_hash"]
                == hashlib.sha256(" ".join(conditional["content"].split()).encode()).hexdigest()
            )
            assert conditional["embedding_is_null"]
            assert conditional["embedding_profile_id"] is None
            assert conditional["search_document_matches"]
            assert conditional["version"] == 2
            assert conditional["updated_at"] > fixture.now
            negated = preserved_rows["memory_0027_negated"]
            assert negated["content"] == negated_content + "\n\n原立场：negated"
            assert (
                negated["content_hash"]
                == hashlib.sha256(" ".join(negated["content"].split()).encode()).hexdigest()
            )
            assert negated["embedding_is_null"]
            assert negated["embedding_profile_id"] is None
            assert negated["search_document_matches"]
            assert negated["version"] == 2
            assert negated["updated_at"] > fixture.now
            plain = preserved_rows["memory_0027_plain"]
            assert plain["content"] == plain_content
            assert plain["content_hash"] == "6" * 64
            assert not plain["embedding_is_null"]
            assert plain["embedding_profile_id"] == "bge-m3-1024-v1"
            assert plain["search_document_matches"]
            assert plain["version"] == 1
            assert plain["updated_at"] == fixture.now
            deleted = preserved_rows["memory_0027_deleted"]
            assert deleted["content"] == ""
            assert deleted["content_hash"] is None
            assert deleted["embedding_is_null"]
            assert deleted["embedding_profile_id"] is None
            assert deleted["version"] == 2
            assert deleted["updated_at"] == fixture.now
            repository = MemoryRepository(harness.factory)
            revalidated = await repository.revalidate_references(
                AccessContext(
                    fixture.owner_id,
                    ("personal",),
                    ("personal",),
                ),
                (("memory_0027_conditional", 1, "personal"),),
            )
            assert revalidated.read_enabled
            assert revalidated.items == {}
            for status in ("done", "queued", "running", "failed"):
                job = jobs[f"job_extract_{status}"]
                assert job["status"] == status
                parsed = ExtractJobResult.model_validate(json.loads(job["result"]))
                assert parsed.result.created == 1
                assert parsed.result.updated == 3
                assert parsed.result.ignored == 0
                assert parsed.result.skipped == 4
                assert parsed.result.batch_id == "batch_old"
                assert parsed.result.run_ids == (fixture.run_ids[0],)

            await harness.downgrade_to("20261125_0026")
            async with harness.factory() as session:
                restored_columns = set(
                    (
                        await session.scalars(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = current_schema() "
                                "AND table_name = 'memories'"
                            )
                        )
                    ).all()
                )
                restored_values = (
                    (
                        await session.execute(
                            text(
                                "SELECT subject_key, fact_key, fact_value, cardinality, "
                                "polarity, applicability, applicability_key, "
                                "normalization_version FROM memories "
                                "WHERE id = 'memory_0027'"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                restored_job = (
                    (
                        await session.execute(
                            text(
                                "SELECT CAST(result AS text) AS result FROM memory_jobs "
                                "WHERE id = 'job_extract_running'"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                restored_control = (
                    (
                        await session.execute(
                            text(
                                "SELECT CAST(payload AS text) AS payload, "
                                "CAST(result AS text) AS result FROM memory_jobs "
                                "WHERE id = 'job_control'"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )

            assert _FACT_COLUMNS <= restored_columns
            assert all(value is None for value in restored_values.values())
            restored_payload = json.loads(restored_job["result"])["result"]
            assert "updated" not in restored_payload
            assert "ignored" not in restored_payload
            assert restored_payload["replaced"] == 3
            assert restored_payload["merged"] == 0
            assert restored_payload["skipped"] == 4
            assert json.loads(restored_control["payload"]) == control_payload
            restored_control_payload = json.loads(restored_control["payload"])
            assert restored_control_payload["arguments_digest"] == "a" * 64
            assert json.loads(restored_control["result"]) == control_result

    asyncio.run(scenario())
