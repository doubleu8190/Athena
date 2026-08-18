"""Long-term memory service coordinating keyword and vector stores."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from athena.config.settings import Settings
from athena.core.memory.ports import MemoryRepository, MemoryVectorStore
from athena.utils.ids import generate_time_id
from athena.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _AccessStat:
    count: int = 1
    last_accessed: str = ""


class MemoryManager:
    """Application service for dual-write long-term memory.

    SQLite is the source of truth for lifecycle and keyword search. Chroma is
    a replaceable vector index. Cross-store operations use explicit
    compensation because no atomic transaction spans both technologies.
    """

    def __init__(
        self,
        settings: Settings,
        repository: MemoryRepository,
        vector_store: MemoryVectorStore,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._vectors = vector_store
        self._initialized = False
        self._access_stats: dict[str, _AccessStat] = {}

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._vectors.initialize()
        self._initialized = True
        logger.info("memory_manager_initialized", path=str(self._settings.chroma_path))

    def _record_access(self, memory_id: str) -> None:
        now = datetime.now().isoformat()
        stat = self._access_stats.get(memory_id)
        if stat is None:
            self._access_stats[memory_id] = _AccessStat(last_accessed=now)
        else:
            stat.count += 1
            stat.last_accessed = now

    def pending_access_stats(self, ids: Iterable[str]) -> dict[str, tuple[int, str]]:
        return {
            memory_id: (self._access_stats[memory_id].count,
                        self._access_stats[memory_id].last_accessed)
            for memory_id in ids if memory_id in self._access_stats
        }

    async def flush_access_stats(self) -> int:
        stats = dict(self._access_stats)
        self._access_stats.clear()
        if not stats:
            return 0
        expires_at = (
            datetime.now() + timedelta(days=self._settings.memory_ttl_days)
        ).isoformat()
        try:
            rows = await self._repository.flush_access_stats(stats, expires_at)
        except Exception:
            self._access_stats.update(stats)
            logger.exception("memory_flush_sqlite_failed")
            raise
        try:
            for row in rows:
                await self._vectors.update(row["id"], metadata={
                    "last_accessed": row["last_accessed"],
                    "access_count": row["access_count"],
                    "expires_at": "" if row["pinned"] else (row["expires_at"] or ""),
                })
        except Exception:
            logger.exception("memory_flush_chromadb_failed")
            raise
        return len(stats)

    async def run_periodic_flush(self) -> None:
        while True:
            await asyncio.sleep(self._settings.memory_sync_interval)
            try:
                await self.flush_access_stats()
            except Exception:
                logger.exception("memory_periodic_flush_failed")
            try:
                await self.cleanup_expired()
            except Exception:
                logger.exception("memory_periodic_cleanup_failed")

    async def add_memory(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
        pinned: bool = False,
    ) -> str:
        await self.initialize()
        metadata = metadata or {}
        if not metadata.get("session_id"):
            logger.warning("add_memory_missing_session_id")
        reserved = {"created_at", "last_accessed", "access_count", "pinned", "expires_at"}
        metadata = {key: value for key, value in metadata.items() if key not in reserved}
        memory_id = generate_time_id()
        now = datetime.now().isoformat()
        expires_at = None if pinned else (
            datetime.now() + timedelta(days=self._settings.memory_ttl_days)
        ).isoformat()
        record = {
            "id": memory_id, "content": content, "metadata": metadata,
            "pinned": pinned, "expires_at": expires_at, "created_at": now,
        }
        await self._repository.add(record)
        vector_metadata = {
            "created_at": now, "last_accessed": now, "access_count": 0,
            "pinned": pinned, "expires_at": expires_at or "", **metadata,
        }
        try:
            await self._vectors.add(memory_id, content, vector_metadata)
        except Exception:
            await self._repository.hard_delete(memory_id)
            logger.exception("memory_add_vector_failed", memory_id=memory_id)
            raise
        return memory_id

    async def search(
        self,
        query: str,
        n_results: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        try:
            results = await self._vectors.query(query, n_results, where)
        except Exception as exc:
            logger.error("memory_search_failed", error=str(exc))
            return []
        ids = results.get("ids") or []
        if not ids or not ids[0]:
            return []
        output: list[dict[str, Any]] = []
        for memory_id, document, metadata, distance in zip(
            ids[0], (results.get("documents") or [[]])[0],
            (results.get("metadatas") or [[]])[0],
            (results.get("distances") or [[]])[0],
        ):
            output.append({
                "id": memory_id, "content": document, "metadata": metadata or {},
                "score": max(0.0, 1.0 - float(distance) / 2.0), "source": "vector",
            })
            self._record_access(memory_id)
        return output

    async def keyword_search(
        self,
        query: str,
        n_results: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            results = await self._repository.keyword_search(query, n_results, where)
        except Exception as exc:
            logger.error("memory_keyword_search_failed", error=str(exc))
            return []
        for item in results:
            self._record_access(item["id"])
        return results

    async def get(self, memory_id: str) -> dict[str, Any] | None:
        await self.initialize()
        try:
            result = await self._vectors.get(memory_id)
        except Exception as exc:
            logger.error("memory_get_failed", memory_id=memory_id, error=str(exc))
            return None
        if not result or not result.get("ids"):
            return None
        self._record_access(memory_id)
        return {
            "id": result["ids"][0],
            "content": (result.get("documents") or [""])[0],
            "metadata": (result.get("metadatas") or [{}])[0],
        }

    async def list_memories(
        self, limit: int = 50, offset: int = 0, pinned_only: bool = False,
        expired_only: bool = False, session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._repository.list(
            limit=limit, offset=offset, pinned_only=pinned_only,
            expired_only=expired_only, session_id=session_id,
        )

    async def count_memories(self) -> dict[str, int]:
        return await self._repository.counts()

    async def update_memory(self, memory_id: str, content: str) -> bool:
        await self.initialize()
        previous = await self._repository.update_content(memory_id, content)
        if previous is None:
            return False
        try:
            await self._vectors.update(memory_id, content=content)
        except Exception:
            await self._repository.update_content(memory_id, previous)
            logger.exception("memory_update_vector_failed", memory_id=memory_id)
            raise
        return True

    async def delete(self, memory_id: str) -> None:
        await self.initialize()
        await self._repository.soft_delete([memory_id])
        try:
            await self._vectors.delete([memory_id])
        except Exception:
            await self._repository.restore_deleted([memory_id])
            logger.exception("memory_delete_vector_failed", memory_id=memory_id)
            raise

    async def pin(self, memory_id: str, pinned: bool = True) -> None:
        await self.initialize()
        expires_at = None if pinned else (
            datetime.now() + timedelta(days=self._settings.memory_ttl_days)
        ).isoformat()
        previous = await self._repository.set_pin(memory_id, pinned, expires_at)
        if previous is None:
            return
        try:
            await self._vectors.update(memory_id, metadata={
                "pinned": pinned, "expires_at": expires_at or "",
            })
        except Exception:
            await self._repository.set_pin(memory_id, previous[0], previous[1])
            logger.exception("memory_pin_vector_failed", memory_id=memory_id)
            raise

    async def cleanup_expired(self) -> int:
        await self.initialize()
        ids = await self._repository.expired_ids(datetime.now().isoformat())
        if not ids:
            return 0
        await self._repository.soft_delete(ids)
        try:
            await self._vectors.delete(ids)
        except Exception:
            await self._repository.restore_deleted(ids)
            logger.exception("memory_cleanup_vector_failed")
            raise
        return len(ids)
