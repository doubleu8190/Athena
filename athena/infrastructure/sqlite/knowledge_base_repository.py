"""独立知识库的 SQLite 持久化。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update

from athena.infrastructure.sqlite.engine import get_session
from athena.infrastructure.sqlite.models import (
    AttachmentModel,
    KnowledgeBaseModel,
)
from athena.models.file import AttachmentStatus, KnowledgeBase
from athena.utils.ids import generate_time_id


def _now_iso() -> str:
    """返回本地当前时间的 ISO 字符串。

    返回值：
        str: 可直接写入现有 SQLite 时间字段的字符串。

    异常：
        不主动抛出业务异常。
    """
    return datetime.now().isoformat()


class KnowledgeBaseRepository:
    """管理全局知识库元数据。"""

    async def create(self, name: str, description: str = "") -> KnowledgeBase:
        """创建一个空知识库。

        参数：
            name (str): 已去除首尾空白的知识库名称。
            description (str): 可选用途说明。

        返回值：
            KnowledgeBase: 新创建的知识库。

        异常：
            数据库写入失败时传播底层异常。
        """
        now = _now_iso()
        row = KnowledgeBaseModel(
            id=generate_time_id(),
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
        )
        async with get_session() as session:
            async with session.begin():
                session.add(row)
        return self._to_domain(row, [])

    async def get(self, knowledge_base_id: str) -> KnowledgeBase | None:
        """读取一个未删除的知识库及其文档统计。

        参数：
            knowledge_base_id (str): 知识库唯一标识。

        返回值：
            KnowledgeBase | None: 知识库不存在或已删除时返回 ``None``。

        异常：
            数据库读取失败时传播底层异常。
        """
        async with get_session() as session:
            row = (
                await session.execute(
                    select(KnowledgeBaseModel).where(
                        KnowledgeBaseModel.id == knowledge_base_id,
                        KnowledgeBaseModel.deleted_time.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            documents = (
                await session.execute(
                    select(AttachmentModel).where(
                        AttachmentModel.knowledge_base_id == knowledge_base_id,
                        AttachmentModel.deleted_time.is_(None),
                    )
                )
            ).scalars().all()
            return self._to_domain(row, documents)

    async def list_all(self) -> list[KnowledgeBase]:
        """列出所有未删除知识库并附带文档统计。

        返回值：
            list[KnowledgeBase]: 按最近更新时间降序排列的知识库。

        异常：
            数据库读取失败时传播底层异常。
        """
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(KnowledgeBaseModel)
                    .where(KnowledgeBaseModel.deleted_time.is_(None))
                    .order_by(KnowledgeBaseModel.updated_at.desc())
                )
            ).scalars().all()
            documents = (
                await session.execute(
                    select(AttachmentModel).where(
                        AttachmentModel.knowledge_base_id.is_not(None),
                        AttachmentModel.deleted_time.is_(None),
                    )
                )
            ).scalars().all()
        documents_by_knowledge_base: dict[str, list[AttachmentModel]] = {}
        for document in documents:
            if document.knowledge_base_id is not None:
                documents_by_knowledge_base.setdefault(
                    document.knowledge_base_id, []
                ).append(document)
        return [
            self._to_domain(row, documents_by_knowledge_base.get(row.id, []))
            for row in rows
        ]

    async def update(
        self, knowledge_base_id: str, name: str, description: str
    ) -> KnowledgeBase | None:
        """更新知识库名称和说明。

        参数：
            knowledge_base_id (str): 知识库唯一标识。
            name (str): 新名称。
            description (str): 新说明。

        返回值：
            KnowledgeBase | None: 更新后的知识库；目标不存在时返回 ``None``。

        异常：
            数据库写入失败时传播底层异常。
        """
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(KnowledgeBaseModel)
                    .where(
                        KnowledgeBaseModel.id == knowledge_base_id,
                        KnowledgeBaseModel.deleted_time.is_(None),
                    )
                    .values(name=name, description=description, updated_at=_now_iso())
                )
        return await self.get(knowledge_base_id)

    async def soft_delete(self, knowledge_base_id: str) -> bool:
        """软删除知识库并移除所有会话绑定。

        参数：
            knowledge_base_id (str): 知识库唯一标识。

        返回值：
            bool: 找到并删除目标时为 ``True``。

        异常：
            数据库写入失败时传播底层异常。
        """
        now = _now_iso()
        async with get_session() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(KnowledgeBaseModel).where(
                            KnowledgeBaseModel.id == knowledge_base_id,
                            KnowledgeBaseModel.deleted_time.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    return False
                row.deleted_time = now
                row.updated_at = now
                return True

    @staticmethod
    def _to_domain(
        row: KnowledgeBaseModel, documents: list[AttachmentModel]
    ) -> KnowledgeBase:
        """把 ORM 行和文档集合转换为带统计信息的领域模型。

        参数：
            row (KnowledgeBaseModel): 知识库 ORM 行。
            documents (list[AttachmentModel]): 当前知识库未删除的文档行。

        返回值：
            KnowledgeBase: 可直接返回给 API 的领域模型。

        异常：
            时间字段格式无效时传播 ``ValueError``。
        """
        return KnowledgeBase(
            id=row.id,
            name=row.name,
            description=row.description,
            document_count=len(documents),
            ready_document_count=sum(
                document.status == AttachmentStatus.READY.value
                for document in documents
            ),
            total_size_bytes=sum(document.size_bytes for document in documents),
            created_at=datetime.fromisoformat(row.created_at),
            updated_at=datetime.fromisoformat(row.updated_at),
        )
