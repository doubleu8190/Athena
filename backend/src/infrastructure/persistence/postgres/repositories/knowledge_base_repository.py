"""新 KnowledgeBase PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.files import KnowledgeBase

from ..models import AttachmentModel, KnowledgeBaseModel
from .converters import knowledge_base_domain_to_model, knowledge_base_model_to_domain


class PostgresKnowledgeBaseRepository:
    """通过注入会话工厂管理知识库和文档统计。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def create(self, knowledge_base: KnowledgeBase) -> KnowledgeBase:
        """创建知识库。"""
        async with self._session_factory() as db:
            async with db.begin():
                db.add(knowledge_base_domain_to_model(knowledge_base))
        return knowledge_base

    async def get(self, knowledge_base_id: str) -> KnowledgeBase | None:
        """查询知识库并统计其未删除文档。"""
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(KnowledgeBaseModel).where(
                        KnowledgeBaseModel.id == knowledge_base_id,
                        KnowledgeBaseModel.deleted_time.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            documents = (
                (
                    await db.execute(
                        select(AttachmentModel).where(
                            AttachmentModel.knowledge_base_id == knowledge_base_id,
                            AttachmentModel.deleted_time.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return knowledge_base_model_to_domain(row, documents=documents)

    async def list_all(self) -> list[KnowledgeBase]:
        """按更新时间倒序列出知识库。"""
        async with self._session_factory() as db:
            rows = (
                (
                    await db.execute(
                        select(KnowledgeBaseModel)
                        .where(KnowledgeBaseModel.deleted_time.is_(None))
                        .order_by(KnowledgeBaseModel.updated_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            documents = (
                (
                    await db.execute(
                        select(AttachmentModel).where(
                            AttachmentModel.knowledge_base_id.is_not(None),
                            AttachmentModel.deleted_time.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        by_base: dict[str, list[AttachmentModel]] = {}
        for document in documents:
            if document.knowledge_base_id is not None:
                by_base.setdefault(document.knowledge_base_id, []).append(document)
        return [
            knowledge_base_model_to_domain(row, documents=by_base.get(row.id, []))
            for row in rows
        ]

    async def save(self, knowledge_base: KnowledgeBase) -> KnowledgeBase:
        """更新知识库名称和说明。"""
        async with self._session_factory() as db:
            async with db.begin():
                await db.execute(
                    update(KnowledgeBaseModel)
                    .where(
                        KnowledgeBaseModel.id == knowledge_base.id,
                        KnowledgeBaseModel.deleted_time.is_(None),
                    )
                    .values(
                        name=knowledge_base.name,
                        description=knowledge_base.description,
                        updated_at=knowledge_base.updated_at.isoformat(),
                    )
                )
        return knowledge_base

    async def delete(self, knowledge_base_id: str) -> bool:
        """软删除知识库。"""
        async with self._session_factory() as db:
            async with db.begin():
                result = await db.execute(
                    update(KnowledgeBaseModel)
                    .where(
                        KnowledgeBaseModel.id == knowledge_base_id,
                        KnowledgeBaseModel.deleted_time.is_(None),
                    )
                    .values(deleted_time=datetime.now(timezone.utc).isoformat())
                    .returning(KnowledgeBaseModel.id)
                )
        return result.scalar_one_or_none() is not None
