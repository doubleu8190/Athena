"""长期记忆可见作用域的统一 SQL 条件。"""

from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement

from .models import MemoryModel


def visible_memory_scope_condition(
    session_id: str, project_id: str | None
) -> ColumnElement[bool]:
    """构造当前会话可访问的事实及非事实记忆范围。

    关键词检索与向量检索必须复用同一条件，确保向量 Top-K 在作用域过滤后
    再排序和截断。

    参数：
    - `session_id`（str）：当前会话标识。
    - `project_id`（str | None）：当前会话绑定的项目；None 表示不开放项目记忆。

    返回：
    - ColumnElement[bool]：可直接拼接到 PostgreSQL 查询的可见性条件。

    异常：
    - 无预期业务异常；SQLAlchemy 条件构造错误按当前实现传播。
    """
    metadata = MemoryModel.metadata_json
    fact = metadata["fact"]
    non_fact_scopes = [
        MemoryModel.session_id == session_id,
        MemoryModel.category.in_(("preference", "profile")),
        and_(
            metadata["scope_kind"].astext == "global",
            metadata["scope_id"].astext == "global",
        ),
        and_(
            metadata["scope_kind"].astext == "session",
            metadata["scope_id"].astext == session_id,
        ),
    ]
    scopes = [
        and_(
            fact["scope_kind"].astext == "global",
            fact["scope_id"].astext == "global",
        ),
        and_(
            fact["scope_kind"].astext == "session",
            fact["scope_id"].astext == session_id,
        ),
    ]
    if project_id:
        non_fact_scopes.append(
            and_(
                metadata["scope_kind"].astext == "project",
                metadata["scope_id"].astext == project_id,
            )
        )
        scopes.append(
            and_(
                fact["scope_kind"].astext == "project",
                fact["scope_id"].astext == project_id,
            )
        )
    scopes.append(and_(fact.astext.is_(None), or_(*non_fact_scopes)))
    return or_(*scopes)


__all__ = ["visible_memory_scope_condition"]
