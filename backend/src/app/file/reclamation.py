"""在停写维护窗口内回收未被数据库引用的文件对象。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from domain.file import FileObjectReferenceRepository, FileObjectStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ObjectReclamationResult:
    """一次从桶起点扫描对象后的稳定汇总。"""

    examined_count: int
    candidate_count: int
    referenced_count: int
    eligible_count: int
    deleted_count: int
    failed_count: int


class FileObjectMaintenance:
    """只在操作者确认全部写入者停止后执行对象回收。"""

    def __init__(
        self,
        *,
        store: FileObjectStore,
        references: FileObjectReferenceRepository,
        retention_seconds: int = 86400,
        page_size: int = 1000,
        delete_batch_size: int = 100,
    ) -> None:
        """保存对象存储、引用复核和扫描参数。

        参数:
            store: 平台私有对象存储。
            references: 会话文件、Skill 版本和执行日志的引用仓储。
            retention_seconds: 候选对象的最小年龄，最低为一天。
            page_size: 单次对象存储列举上限。
            delete_batch_size: 单批最多提交的删除数量。

        返回:
            无返回值。

        异常:
            保留期或分页参数越界时抛出 ``ValueError``。
        """
        if retention_seconds < 86400:
            raise ValueError("file_maintenance_grace_period_invalid")
        if not 1 <= page_size <= 1000:
            raise ValueError("file_maintenance_page_size_invalid")
        if not 1 <= delete_batch_size <= 100:
            raise ValueError("file_maintenance_delete_batch_invalid")
        self._store = store
        self._references = references
        self._retention_seconds = retention_seconds
        self._page_size = page_size
        self._delete_batch_size = delete_batch_size

    async def run(
        self,
        *,
        execute: bool = False,
        writers_stopped: bool = False,
    ) -> ObjectReclamationResult:
        """从桶起点扫描全部合法对象，默认只报告预计删除结果。

        参数:
            execute: 为 ``True`` 时删除已确认无数据库引用的候选对象。
            writers_stopped: 操作者对全部写入者已停止的确认；执行删除时必填。

        返回:
            本次扫描、引用命中、可删除和实际删除数量的汇总。

        异常:
            ``execute`` 为真但未确认停写时抛出 ``ValueError``；对象存储列举失败
            会继续抛出，引用查询与单项删除失败则计数并保留对象。
        """
        if execute and not writers_stopped:
            raise ValueError("file_maintenance_writers_must_stop")

        examined = 0
        candidates = 0
        referenced = 0
        eligible = 0
        deleted = 0
        failed = 0
        after_key: str | None = None
        pending_deletes: list[str] = []

        while True:
            entries = []
            iterator = self._store.iter_maintenance_candidates(
                older_than_seconds=self._retention_seconds,
                after_key=after_key,
                maximum_items=self._page_size,
            )
            try:
                async for entry in iterator:
                    entries.append(entry)
            finally:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    await close()
            if not entries:
                break

            for entry in entries:
                examined += 1
                if not entry.is_candidate:
                    continue
                candidates += 1
                try:
                    is_referenced = await self._references.is_object_key_referenced(
                        entry.object_key
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    failed += 1
                    logger.warning(
                        "file_maintenance_reference_check_failed exception_type=%s",
                        type(error).__name__,
                    )
                    continue
                if is_referenced:
                    referenced += 1
                    continue
                eligible += 1
                if not execute:
                    continue
                pending_deletes.append(entry.object_key)
                if len(pending_deletes) >= self._delete_batch_size:
                    deleted, failed = await self._delete_pending(
                        pending_deletes,
                        deleted=deleted,
                        failed=failed,
                    )

            after_key = entries[-1].object_key
            if len(entries) < self._page_size:
                break

        if execute and pending_deletes:
            deleted, failed = await self._delete_pending(
                pending_deletes,
                deleted=deleted,
                failed=failed,
            )
        return ObjectReclamationResult(
            examined_count=examined,
            candidate_count=candidates,
            referenced_count=referenced,
            eligible_count=eligible,
            deleted_count=deleted,
            failed_count=failed,
        )

    async def _delete_pending(
        self,
        object_keys: list[str],
        *,
        deleted: int,
        failed: int,
    ) -> tuple[int, int]:
        """逐项删除一个小批，并清空待处理键列表。

        参数:
            object_keys: 当前批次的可删除对象键。
            deleted: 此前已成功删除的数量。
            failed: 此前失败的数量。

        返回:
            更新后的成功数和失败数。

        异常:
            取消继续向上传播；普通存储错误计入失败并保留对应对象。
        """
        for object_key in object_keys:
            try:
                await self._store.delete(object_key)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failed += 1
                logger.warning(
                    "file_maintenance_delete_failed exception_type=%s safe_code=%s",
                    type(error).__name__,
                    getattr(error, "code", "file_storage_unavailable"),
                )
            else:
                deleted += 1
        object_keys.clear()
        return deleted, failed


__all__ = ["FileObjectMaintenance", "ObjectReclamationResult"]
