"""文件产物和适配器注册表 PostgreSQL repository。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.files import AdapterInfo, FileArtifact

from ..models import AdapterRegistryModel, FileArtifactModel
from .converters import (
    adapter_domain_to_model,
    adapter_model_to_domain,
    file_artifact_domain_to_model,
    file_artifact_model_to_domain,
)


class PostgresFileArtifactRepository:
    """通过注入会话工厂管理文件解析产物。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get(self, cache_key: str) -> FileArtifact | None:
        """按缓存键读取产物。"""
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(FileArtifactModel).where(
                        FileArtifactModel.cache_key == cache_key
                    )
                )
            ).scalar_one_or_none()
        return None if row is None else file_artifact_model_to_domain(row)

    async def put(self, artifact: FileArtifact) -> FileArtifact:
        """插入或更新缓存产物。"""
        async with self._session_factory() as db:
            async with db.begin():
                values = file_artifact_domain_to_model(artifact)
                statement = insert(FileArtifactModel).values(
                    id=values.id,
                    attachment_id=values.attachment_id,
                    kind=values.kind,
                    cache_key=values.cache_key,
                    content=values.content,
                    storage_key=values.storage_key,
                    metadata_json=values.metadata_json,
                    created_at=values.created_at,
                )
                await db.execute(
                    statement.on_conflict_do_update(
                        index_elements=[FileArtifactModel.cache_key],
                        set_={
                            "content": statement.excluded.content,
                            "storage_key": statement.excluded.storage_key,
                            "metadata_json": statement.excluded.metadata_json,
                            "created_at": statement.excluded.created_at,
                        },
                    )
                )
        return artifact


class PostgresAdapterRegistryRepository:
    """通过注入会话工厂同步文件适配器注册表。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def replace_all(self, adapters: list[AdapterInfo]) -> None:
        """用当前适配器集合替换持久化注册表。"""
        now = datetime.now(timezone.utc).isoformat()
        names = [item.name for item in adapters]
        async with self._session_factory() as db:
            async with db.begin():
                if names:
                    await db.execute(
                        delete(AdapterRegistryModel).where(
                            AdapterRegistryModel.name.not_in(names)
                        )
                    )
                else:
                    await db.execute(delete(AdapterRegistryModel))
                for item in adapters:
                    values = adapter_domain_to_model(item, updated_at=now)
                    statement = insert(AdapterRegistryModel).values(
                        name=values.name,
                        version=values.version,
                        mime_types_json=values.mime_types_json,
                        extensions_json=values.extensions_json,
                        capabilities_json=values.capabilities_json,
                        enabled=values.enabled,
                        updated_at=values.updated_at,
                    )
                    await db.execute(
                        statement.on_conflict_do_update(
                            index_elements=[AdapterRegistryModel.name],
                            set_={
                                "version": statement.excluded.version,
                                "mime_types_json": statement.excluded.mime_types_json,
                                "extensions_json": statement.excluded.extensions_json,
                                "capabilities_json": statement.excluded.capabilities_json,
                                "enabled": statement.excluded.enabled,
                                "updated_at": statement.excluded.updated_at,
                            },
                        )
                    )

    async def list_all(self) -> list[AdapterInfo]:
        """读取启用和禁用的适配器注册信息。"""
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(AdapterRegistryModel).order_by(AdapterRegistryModel.name.asc())
                )
            ).scalars().all()
        return [adapter_model_to_domain(row) for row in rows]
