"""Skill ZIP 包有界内存字节 LRU 缓存（single-flight 去重、SHA-256 校验与路径穿越防御）。"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from common.async_utils import await_task_shielded
from common.safe_path import (
    resolve_safe_child_path as _resolve_safe_child_path,
)
from common.safe_path import (
    safe_relative_posix_parts as _safe_relative_posix_parts,
)
from domain.skill import SkillVersion

DEFAULT_SKILL_CACHE_MAX_ENTRIES = 16
DEFAULT_SKILL_CACHE_MAX_TOTAL_BYTES = 64 * 1024 * 1024


@dataclass(slots=True)
class _InFlightFetch:
    """同 object_key 并发冷加载的 single-flight 协调状态。"""

    task: asyncio.Task[tuple[bytes, str]]
    done_event: asyncio.Event
    waiters: int = 0


def safe_relative_posix_parts(path: str) -> tuple[str, ...]:
    """防御性校验资源路径为纯相对 POSIX 路径，并维持既有完整性错误码。"""
    try:
        return _safe_relative_posix_parts(path)
    except ValueError as error:
        raise ValueError("skill_package_integrity_invalid") from error


def resolve_safe_child_path(base_root: Path, relative_path: str) -> Path:
    """将已校验相对路径解析到根目录内并拒绝 symlink，维持既有完整性错误码。"""
    try:
        return _resolve_safe_child_path(base_root, relative_path)
    except ValueError as error:
        raise ValueError("skill_package_integrity_invalid") from error


class SkillPackageCache:
    """不可变 ZIP 技能包有界内存字节 LRU 缓存（single-flight 去重 + 整包 SHA-256 校验）。"""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_SKILL_CACHE_MAX_ENTRIES,
        max_total_bytes: int = DEFAULT_SKILL_CACHE_MAX_TOTAL_BYTES,
    ) -> None:
        if max_entries < 1 or max_total_bytes < 1:
            raise ValueError("skill_package_cache_policy_invalid")
        self._max_entries = max_entries
        self._max_total_bytes = max_total_bytes
        self._lock = asyncio.Lock()
        self._entries: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._total_bytes = 0
        self._inflight: dict[str, _InFlightFetch] = {}
        self._closed = False

    async def get_zip_bytes(
        self,
        version: SkillVersion,
        *,
        fetch_bytes: Callable[[], Awaitable[bytes]],
    ) -> bytes:
        """按 object_key 读取或回源下载 ZIP 字节，校验 package_sha256 并维护有界 LRU。"""
        key = version.object_key
        if key is None:
            raise ValueError("skill_package_integrity_invalid")
        async with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                data, cached_sha256 = cached
                if cached_sha256 != version.package_sha256:
                    raise ValueError("skill_package_integrity_invalid")
                self._entries.move_to_end(key)
                return data
            flight = self._inflight.get(key)
            if flight is None:
                done_event = asyncio.Event()
                task = asyncio.create_task(
                    self._fetch_and_store(key, version.package_sha256, fetch_bytes),
                    name=f"athena-skill-zip-cache-{key[-16:]}",
                )

                # 回源结束时主动消费异常并唤醒全部等待者，避免 asyncio.shield 的
                # ``exception in shielded future`` 与未检索任务异常告警。
                def _on_flight_done(
                    done_task: asyncio.Task[tuple[bytes, str]],
                    *,
                    event: asyncio.Event = done_event,
                ) -> None:
                    _consume_task_exception(done_task)
                    event.set()

                task.add_done_callback(_on_flight_done)
                flight = _InFlightFetch(task=task, done_event=done_event)
                self._inflight[key] = flight
            flight.waiters += 1
        try:
            # 等待 done_event 而非 asyncio.shield(flight.task)：单个等待者取消不会中止
            # 共享回源任务，也不会在任务随后失败时触发 shielded future 告警。
            if not flight.task.done():
                await flight.done_event.wait()
            data, actual_sha256 = flight.task.result()
        except BaseException:
            # 最后一个等待者退出时中止孤立回源任务并等待其收敛，且消费已完成任务异常。
            should_cancel = False
            is_last_waiter = False
            async with self._lock:
                flight.waiters = max(0, flight.waiters - 1)
                if flight.waiters == 0:
                    is_last_waiter = True
                    if self._inflight.get(key) is flight:
                        self._inflight.pop(key, None)
                        should_cancel = True
            if should_cancel and not flight.task.done():
                flight.task.cancel()
                await await_task_shielded(flight.task)
            if is_last_waiter and flight.task.done() and not flight.task.cancelled():
                flight.task.exception()
            raise
        async with self._lock:
            flight.waiters = max(0, flight.waiters - 1)
        if actual_sha256 != version.package_sha256:
            raise ValueError("skill_package_integrity_invalid")
        return data

    async def aclose(self) -> None:
        """取消全部在途回源下载并清空内存缓存，收敛所有异常后透传期间收到的外部取消。"""
        async with self._lock:
            self._closed = True
            tasks = tuple(flight.task for flight in self._inflight.values())
            self._inflight.clear()
            self._entries.clear()
            self._total_bytes = 0
        for task in tasks:
            if not task.done():
                task.cancel()
        cancelled: asyncio.CancelledError | None = None
        for task in tasks:
            cancelled_here = await await_task_shielded(task)
            if cancelled_here is not None:
                cancelled = cancelled_here
            if not task.cancelled():
                task.exception()
        if cancelled is not None:
            raise cancelled

    async def _fetch_and_store(
        self,
        key: str,
        expected_sha256: str,
        fetch_bytes: Callable[[], Awaitable[bytes]],
    ) -> tuple[bytes, str]:
        try:
            data = await fetch_bytes()
            actual_sha256 = hashlib.sha256(data).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError("skill_package_integrity_invalid")
            async with self._lock:
                if not self._closed and len(data) <= self._max_total_bytes:
                    previous = self._entries.pop(key, None)
                    if previous is not None:
                        self._total_bytes -= len(previous[0])
                    self._entries[key] = (data, actual_sha256)
                    self._total_bytes += len(data)
                    while self._entries and (
                        len(self._entries) > self._max_entries
                        or self._total_bytes > self._max_total_bytes
                    ):
                        _, (evicted_data, _) = self._entries.popitem(last=False)
                        self._total_bytes -= len(evicted_data)
            return data, actual_sha256
        finally:
            async with self._lock:
                current = self._inflight.get(key)
                if current is not None and current.task is asyncio.current_task():
                    self._inflight.pop(key, None)


def _consume_task_exception(done_task: asyncio.Task[tuple[bytes, str]]) -> None:
    """读取已完成任务的异常标记，避免孤立失败任务被回收时打印未检索告警。"""
    if not done_task.cancelled():
        done_task.exception()


__all__ = [
    "DEFAULT_SKILL_CACHE_MAX_ENTRIES",
    "DEFAULT_SKILL_CACHE_MAX_TOTAL_BYTES",
    "SkillPackageCache",
    "resolve_safe_child_path",
    "safe_relative_posix_parts",
]
