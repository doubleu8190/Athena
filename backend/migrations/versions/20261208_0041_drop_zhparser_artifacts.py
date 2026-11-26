"""清理存量 PostgreSQL 数据库遗留的 zhparser 扩展与 athena_memory_chinese 分词配置。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261208_0041"
down_revision: str | None = "20261207_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 共享受保护扩展卸载语句：严禁使用 CASCADE（避免误删共享库内其他 schema 依赖对象），
# 同时捕获权限不足（托管云 RDS 非超管）与外部依赖残留异常，确保升级幂等不中断。
_SAFE_DROP_ZHPARSER_EXTENSION_SQL = (
    "DO $$ BEGIN "
    "DROP EXTENSION IF EXISTS zhparser; "
    "EXCEPTION WHEN insufficient_privilege OR dependent_objects_still_exist THEN NULL; "
    "END $$"
)


def _is_postgresql() -> bool:
    """判断当前迁移目标方言是否为 PostgreSQL，兼容在线连接与离线 SQL 生成。"""
    bind = op.get_bind()
    if bind is not None:
        return bind.dialect.name == "postgresql"
    return op.get_context().dialect.name == "postgresql"


def _is_offline() -> bool:
    """判断是否处于 Alembic 离线 SQL 生成模式（无需依赖全局 alembic.context 代理）。"""
    return bool(op.get_context().as_sql)


def upgrade() -> None:
    """仅在 PostgreSQL 方言下幂等清理历史重型记忆遗留的分词配置与 zhparser 扩展。"""
    if not _is_postgresql():
        return

    if _is_offline():
        # 离线 SQL 生成模式无法探测实时 schema 或 pg_extension，
        # 统一使用受异常保护的卸载块，确保离线脚本在非超管或共享库环境同样安全可执行。
        op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS athena_memory_chinese")
        op.execute(_SAFE_DROP_ZHPARSER_EXTENSION_SQL)
        return

    bind = op.get_bind()
    # 先移除当前 schema 下依赖 zhparser 的全文检索配置，再尝试卸载扩展本身；
    # 若异常 search_path 导致 current_schema() 返回 None/空串，跳过限定删除避免拼出非法 "None"。
    raw_schema: object = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    schema = raw_schema if isinstance(raw_schema, str) and raw_schema else None
    if schema:
        quote = bind.dialect.identifier_preparer.quote
        config_name = f"{quote(schema)}.{quote('athena_memory_chinese')}"
        op.execute(f"DROP TEXT SEARCH CONFIGURATION IF EXISTS {config_name}")

    # 全新无 zhparser 数据库直接跳过；存量库存在扩展时执行受保护卸载块。
    has_zhparser = bool(
        bind.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'zhparser')")
        ).scalar_one()
    )
    if has_zhparser:
        op.execute(_SAFE_DROP_ZHPARSER_EXTENSION_SQL)


def downgrade() -> None:
    """历史重型记忆表已于 20261130_0033 下线，降级保持空操作，不再重建 zhparser 遗留对象。"""
    return
