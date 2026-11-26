"""移除记忆事实身份字段，改由检索候选决定新增或替代。

部署时先停止应用与 Worker，完成迁移后再启动，避免跨版本进程并行读写。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261126_0027"
down_revision: str | None = "20261125_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FACT_IDENTITY_COLUMNS = (
    "normalization_version",
    "applicability_key",
    "applicability",
    "polarity",
    "cardinality",
    "fact_value",
    "fact_key",
    "subject_key",
)


def _json_type() -> sa.types.TypeEngine[object]:
    """让降级恢复的适用条件列继续使用 PostgreSQL JSONB。"""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """保留记忆正文与来源，只移除已废弃的事实身份协议结构。"""
    # 旧正文并不保证包含 applicability 或否定立场；删除结构列前，把真实限定
    # 以原始 JSON 普通文本附回正文。空默认对象不追加，墓碑不补任何内容。
    op.execute(
        "WITH preserved AS ("
        "SELECT id, content || E'\\n\\n' || "
        "CASE WHEN applicability IS NOT NULL AND ("
        "COALESCE(BTRIM(applicability ->> 'topic'), '') <> '' "
        "OR COALESCE(jsonb_array_length(applicability -> 'conditions'), 0) > 0 "
        "OR NULLIF(BTRIM(applicability ->> 'valid_from'), '') IS NOT NULL "
        "OR NULLIF(BTRIM(applicability ->> 'valid_to'), '') IS NOT NULL "
        "OR (applicability - 'topic' - 'conditions' - 'valid_from' - 'valid_to') "
        "<> '{}'::jsonb"
        ") THEN "
        "E'历史限定（原始 applicability JSON）：' || applicability::text "
        "ELSE '' END || "
        "CASE WHEN polarity = 'negated' THEN "
        "CASE WHEN applicability IS NOT NULL AND ("
        "COALESCE(BTRIM(applicability ->> 'topic'), '') <> '' "
        "OR COALESCE(jsonb_array_length(applicability -> 'conditions'), 0) > 0 "
        "OR NULLIF(BTRIM(applicability ->> 'valid_from'), '') IS NOT NULL "
        "OR NULLIF(BTRIM(applicability ->> 'valid_to'), '') IS NOT NULL "
        "OR (applicability - 'topic' - 'conditions' - 'valid_from' - 'valid_to') "
        "<> '{}'::jsonb"
        ") THEN E'\\n' ELSE '' END || "
        "E'原立场：' || polarity "
        "ELSE '' END AS preserved_content "
        "FROM memories "
        "WHERE deleted_time IS NULL AND ("
        "polarity = 'negated' "
        "OR (applicability IS NOT NULL AND ("
        "COALESCE(BTRIM(applicability ->> 'topic'), '') <> '' "
        "OR COALESCE(jsonb_array_length(applicability -> 'conditions'), 0) > 0 "
        "OR NULLIF(BTRIM(applicability ->> 'valid_from'), '') IS NOT NULL "
        "OR NULLIF(BTRIM(applicability ->> 'valid_to'), '') IS NOT NULL "
        "OR (applicability - 'topic' - 'conditions' - 'valid_from' - 'valid_to') "
        "<> '{}'::jsonb"
        "))"
        ")) "
        "UPDATE memories AS memory SET "
        "content = preserved.preserved_content, "
        # 历史重型记忆表已下线并移除 zhparser 依赖，此处改用 PostgreSQL 内置 simple 分词配置。
        "search_document = to_tsvector("
        "'simple', preserved.preserved_content"
        "), "
        "content_hash = encode(sha256(convert_to("
        "regexp_replace(BTRIM(preserved.preserved_content), "
        "'[[:space:]]+', ' ', 'g')"
        ", 'UTF8')), 'hex'), "
        "embedding = NULL, "
        "embedding_profile_id = NULL, "
        "version = memory.version + 1, "
        "updated_at = CURRENT_TIMESTAMP "
        "FROM preserved "
        "WHERE memory.id = preserved.id"
    )
    op.drop_index("ix_memories_fact_group", table_name="memories")
    op.drop_constraint("ck_memories_polarity", "memories", type_="check")
    op.drop_constraint("ck_memories_cardinality", "memories", type_="check")
    for column_name in _FACT_IDENTITY_COLUMNS:
        op.drop_column("memories", column_name)
    # 旧提取结果仍可严格校验；已完成 Run 只做无正文 JSON 投影，绝不重新入队。
    op.execute(
        "UPDATE memory_jobs SET result = jsonb_build_object("
        "'kind', result -> 'kind', "
        "'result', ((result -> 'result') - 'merged' - 'replaced') "
        "|| jsonb_build_object("
        "'updated', COALESCE((result #>> '{result,merged}')::integer, 0) "
        "+ COALESCE((result #>> '{result,replaced}')::integer, 0), "
        "'ignored', 0"
        ")) "
        "WHERE kind = 'extract_pending' "
        "AND jsonb_typeof(result -> 'result') = 'object' "
        "AND (result #> '{result,merged}' IS NOT NULL "
        "OR result #> '{result,replaced}' IS NOT NULL)"
    )


def downgrade() -> None:
    """恢复旧结构的空列形状；已删除的身份内容保持为空且不会重建。"""
    op.add_column("memories", sa.Column("subject_key", sa.String(200), nullable=True))
    op.add_column("memories", sa.Column("fact_key", sa.String(128), nullable=True))
    op.add_column("memories", sa.Column("fact_value", sa.String(500), nullable=True))
    op.add_column("memories", sa.Column("cardinality", sa.String(16), nullable=True))
    op.add_column("memories", sa.Column("polarity", sa.String(16), nullable=True))
    op.add_column("memories", sa.Column("applicability", _json_type(), nullable=True))
    op.add_column("memories", sa.Column("applicability_key", sa.String(64), nullable=True))
    op.add_column("memories", sa.Column("normalization_version", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_memories_cardinality",
        "memories",
        "cardinality IS NULL OR cardinality IN ('single','set','unknown')",
    )
    op.create_check_constraint(
        "ck_memories_polarity",
        "memories",
        "polarity IS NULL OR polarity IN ('affirmed','negated')",
    )
    op.create_index(
        "ix_memories_fact_group",
        "memories",
        ["namespace_id", "scope_key", "subject_key", "fact_key", "applicability_key"],
    )
    # 旧严格结果结构没有 updated/ignored；降级把新更新投影为 replaced，
    # 不改变已完成状态，也不触发任何 Run 或控制回执重放。
    op.execute(
        "UPDATE memory_jobs SET result = jsonb_build_object("
        "'kind', result -> 'kind', "
        "'result', ((result -> 'result') - 'updated' - 'ignored') "
        "|| jsonb_build_object("
        "'merged', 0, "
        "'replaced', COALESCE((result #>> '{result,updated}')::integer, 0), "
        "'skipped', COALESCE((result #>> '{result,skipped}')::integer, 0) "
        "+ COALESCE((result #>> '{result,ignored}')::integer, 0)"
        ")) "
        "WHERE kind = 'extract_pending' "
        "AND jsonb_typeof(result -> 'result') = 'object' "
        "AND (result #> '{result,updated}' IS NOT NULL "
        "OR result #> '{result,ignored}' IS NOT NULL)"
    )
